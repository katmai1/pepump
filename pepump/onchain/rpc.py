"""Ayudas comunes para hablar con el RPC de Solana: traducir sus errores y
getProgramAccounts con fallback a la variante paginada (V2)."""
import asyncio
import base64
import logging
from typing import Any

import requests
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)


class RpcErrorResponse(RuntimeError):
    """El nodo RPC contestó con un error JSON-RPC en vez de con datos."""


def rpc_value(resp, que: str):
    """El `.value` de una respuesta de solana-py, pero traduciendo el
    error del nodo a algo legible.

    BUGFIX: estas respuestas NO siempre traen `.value`. Cuando el nodo
    contesta con un error JSON-RPC, solana-py devuelve un objeto de
    `solders.rpc.errors` (InvalidRequestMessage, etc.) que no tiene ese
    campo, así que el `resp.value` de siempre reventaba con
    "'InvalidRequestMessage' object has no attribute 'value'". El
    `except Exception` del llamador logueaba ese AttributeError tal cual
    y TAPABA el motivo real, que el nodo sí había explicado. Caso real:
    Helius respondiendo "Too many accounts requested (10000001 pubkeys),
    Please use getProgramAccountsV2 with pagination" a un
    getProgramAccounts sobre el programa de PumpSwap -el bot solo decía
    "no hay ninguna fuente de precio disponible" y abortaba la entrada,
    sin ninguna pista de que el problema era el RPC."""
    if hasattr(resp, "value"):
        return resp.value
    message = getattr(resp, "message", None) or repr(resp)
    raise RpcErrorResponse(f"el RPC rechazó {que}: {message}")


# Marcas en el mensaje de error con las que un proveedor está pidiendo la
# variante paginada en vez del getProgramAccounts clásico.
_NEEDS_GPA_V2_HINTS = ("getprogramaccountsv2", "pagination")
# Cuentas por página al usar la V2 (10.000 es el máximo que acepta
# Helius: con más responde "Pagination limit too large"), y techo de
# páginas como red de seguridad para no quedar en un bucle infinito si un
# nodo devolviera siempre la misma paginationKey.
_GPA_V2_PAGE_LIMIT = 10_000
_GPA_V2_MAX_PAGES = 200


class _CuentaCruda:
    """Cuenta de getProgramAccountsV2 con la MISMA forma que las que
    devuelve solana-py (`.pubkey` y `.account.data`), para que el parseo
    de los clientes de abajo funcione igual venga de donde venga."""

    __slots__ = ("account", "pubkey")

    class _Datos:
        __slots__ = ("data",)

        def __init__(self, data: bytes):
            self.data = data

    def __init__(self, pubkey: str, data: bytes):
        self.pubkey = pubkey
        self.account = self._Datos(data)


async def get_program_accounts(client: AsyncClient, rpc_url: str, program_id: Pubkey,
                                 filters: list, que: str) -> list:
    """getProgramAccounts con `filters`, cayendo a getProgramAccountsV2
    (paginada) si el proveedor rechaza la clásica.

    Algunos proveedores ya no sirven el getProgramAccounts estándar sobre
    programas enormes. Helius responde "Too many accounts requested
    (10000001 pubkeys), Please use getProgramAccountsV2 with pagination"
    para el programa de PumpSwap, y lo decide por el tamaño del programa
    ANTES de aplicar los memcmp, así que afinar los filtros no sirve de
    nada: con el endpoint público pasa algo parecido (getProgramAccounts
    directamente limitado). Sin este fallback, un RPC así dejaba al bot
    sin NINGUNA forma de encontrar el pool.

    La V2 es una extensión del proveedor, no RPC estándar de Solana, y
    solana-py no la expone: se manda a mano con el mismo `requests` en un
    thread que ya usa la Lightning API. El camino normal sigue siendo la
    clásica -la V2 solo se intenta si la otra falla- para no romper los
    RPC que no la tienen (nodo propio, endpoint público)."""
    try:
        resp = await client.get_program_accounts(program_id, encoding="base64", filters=filters)
        return rpc_value(resp, que) or []
    except RpcErrorResponse as e:
        if not any(hint in str(e).lower() for hint in _NEEDS_GPA_V2_HINTS):
            raise
        logger.debug(f"[On-chain] {e} -> reintento con getProgramAccountsV2 paginada.")

    return await _get_program_accounts_v2(rpc_url, program_id, filters, que)


async def _get_program_accounts_v2(rpc_url: str, program_id: Pubkey,
                                    filters: list, que: str) -> list:
    """getProgramAccountsV2 recorriendo TODAS las páginas (ver
    get_program_accounts, que es quien decide cuándo usar esto)."""
    # OJO: la paginación recorre el conjunto CRUDO de cuentas del
    # programa y aplica los filtros a cada página, así que una página
    # vacía es normal y NO significa que se terminó -solo la ausencia de
    # paginationKey marca el final. Comprobado contra Helius: la primera
    # página vuelve vacía y el pool aparece en la segunda.
    filtros_json = [{"memcmp": {"offset": f.offset, "bytes": f.bytes}} for f in filters]
    cuentas: list = []
    pagination_key = None

    for _pagina in range(_GPA_V2_MAX_PAGES):
        config: dict[str, Any] = {"encoding": "base64", "limit": _GPA_V2_PAGE_LIMIT, "filters": filtros_json}
        if pagination_key:
            config["paginationKey"] = pagination_key
        resp = await asyncio.to_thread(
            requests.post, rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getProgramAccountsV2",
                  "params": [str(program_id), config]},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RpcErrorResponse(f"el RPC devolvió {resp.status_code} en getProgramAccountsV2 "
                                   f"({que}): {resp.text[:200]}")
        body = resp.json()
        if "error" in body:
            raise RpcErrorResponse(f"el RPC rechazó getProgramAccountsV2 ({que}): "
                                   f"{body['error'].get('message', body['error'])}")

        # La V2 devuelve {"accounts": [...], "paginationKey": ..., "count": N};
        # se acepta también una lista pelada por si otro proveedor la sirve así.
        result = body.get("result") or {}
        if isinstance(result, dict):
            pagina = result.get("accounts") or []
            pagination_key = result.get("paginationKey")
        else:
            pagina, pagination_key = result, None

        for acc in pagina:
            data = (acc.get("account") or {}).get("data")
            # data = ["<base64>", "base64"]; si viniera en otro formato
            # (jsonParsed) la saltamos en vez de reventar.
            if not isinstance(data, list) or not data:
                continue
            try:
                cuentas.append(_CuentaCruda(acc["pubkey"], base64.b64decode(data[0])))
            except (KeyError, ValueError, TypeError):
                continue

        if not pagination_key:
            return cuentas

    # OJO: aquí se LANZA, no se devuelven las cuentas leídas hasta ahora.
    # Un recorrido truncado no es una respuesta: el llamador interpreta
    # "lista vacía" como `pool_confirmed_absent=True` -una confirmación
    # de que el mint no tiene pool-, y con un barrido a medias eso sería
    # mentira. Mejor un fallo explícito, que el llamador ya trata como
    # "no se sabe" y reintenta, que una confirmación falsa.
    raise RpcErrorResponse(f"getProgramAccountsV2 ({que}) no terminó de paginar en "
                           f"{_GPA_V2_MAX_PAGES} páginas de {_GPA_V2_PAGE_LIMIT}; "
                           f"no se puede dar la respuesta por completa")
