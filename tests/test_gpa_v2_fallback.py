"""
Tests del fallback a getProgramAccountsV2 y de los errores del RPC que
antes quedaban tapados (ver pump.py: _rpc_value / _get_program_accounts).

Caso real que motivó todo esto: Helius dejó de servir el
getProgramAccounts clásico sobre el programa de PumpSwap (10M de cuentas)
y responde "Too many accounts requested (10000001 pubkeys), Please use
getProgramAccountsV2 with pagination". El bot hacía `resp.value` sobre esa
respuesta de error -que no tiene ese campo- y el AttributeError resultante
se logueaba tal cual, así que el usuario solo veía
"'InvalidRequestMessage' object has no attribute 'value'" y después
"no hay ninguna fuente de precio disponible", sin ninguna pista del
motivo real ni forma de conseguir el precio.
"""
import base64

import pytest
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey

from pepump import pump as pump_module
from pepump.pump import (PUMPSWAP_PROGRAM_ID, RpcErrorResponse, _get_program_accounts,
                          _rpc_value)

PROGRAM = Pubkey.from_string(PUMPSWAP_PROGRAM_ID)
FILTROS = [MemcmpOpts(offset=43, bytes="So11111111111111111111111111111111111111112")]
MENSAJE_HELIUS = ("Too many accounts requested (10000001 pubkeys), Please use "
                  "getProgramAccountsV2 with pagination to handle large datasets.")


class RespuestaDeError:
    """Imita a solders.rpc.errors.InvalidRequestMessage: trae `.message`
    pero NO `.value`, que es justo lo que reventaba."""

    def __init__(self, message: str):
        self.message = message


class RespuestaOk:
    def __init__(self, value):
        self.value = value


class FakeClient:
    """Doble de AsyncClient para get_program_accounts: devuelve lo que se
    le indique (una respuesta de error o una normal)."""

    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.calls = 0

    async def get_program_accounts(self, program, encoding=None, filters=None):
        self.calls += 1
        return self.respuesta


class FakeHttpResponse:
    def __init__(self, body, status_code=200, text=""):
        self._body = body
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._body


def pagina(cuentas, pagination_key=None):
    """Una página de getProgramAccountsV2 tal como la sirve Helius."""
    return {"result": {"accounts": [{"pubkey": pk,
                                      "account": {"data": [base64.b64encode(d).decode(), "base64"]}}
                                     for pk, d in cuentas],
                        "paginationKey": pagination_key,
                        "count": len(cuentas)}}


@pytest.fixture
def fake_post(monkeypatch):
    """Intercepta el requests.post que usa _get_program_accounts_v2 y va
    devolviendo las páginas que se le pasen, registrando cada llamada."""
    def _instalar(paginas):
        hechas = []

        def _post(url, json=None, timeout=None):
            hechas.append(json)
            return FakeHttpResponse(paginas[len(hechas) - 1])

        monkeypatch.setattr(pump_module.requests, "post", _post)
        return hechas
    return _instalar


# --- _rpc_value: el error del RPC deja de quedar tapado ---------------- #

def test_rpc_value_devuelve_el_valor_si_la_respuesta_es_normal():
    assert _rpc_value(RespuestaOk([1, 2, 3]), "algo") == [1, 2, 3]
    assert _rpc_value(RespuestaOk(None), "algo") is None


def test_rpc_value_traduce_el_error_del_nodo_en_vez_de_un_attributeerror():
    """Antes: AttributeError sin información. Ahora: el mensaje del nodo,
    que es el que explica de verdad qué pasó."""
    with pytest.raises(RpcErrorResponse) as exc:
        _rpc_value(RespuestaDeError(MENSAJE_HELIUS), "los pools de PumpSwap de X")
    assert "los pools de PumpSwap de X" in str(exc.value)
    assert "getProgramAccountsV2" in str(exc.value)


# --- _get_program_accounts: camino normal y fallback a la V2 ----------- #

async def test_usa_la_clasica_cuando_el_rpc_la_acepta(fake_post):
    """Si getProgramAccounts funciona, no se toca la V2: los RPC que no la
    tienen (nodo propio, endpoint público) deben seguir yendo igual."""
    hechas = fake_post([])
    client = FakeClient(RespuestaOk(["cuenta-a", "cuenta-b"]))

    assert await _get_program_accounts(client, "http://rpc.test", PROGRAM, FILTROS, "x") == \
        ["cuenta-a", "cuenta-b"]
    assert client.calls == 1
    assert hechas == []  # no se mandó ningún getProgramAccountsV2


async def test_cae_a_la_v2_cuando_el_rpc_pide_paginacion(fake_post):
    """El fix: el rechazo de Helius dispara el reintento con la V2, y el
    pool se encuentra igual."""
    hechas = fake_post([pagina([("Pool1111", b"datos-del-pool")])])
    client = FakeClient(RespuestaDeError(MENSAJE_HELIUS))

    cuentas = await _get_program_accounts(client, "http://rpc.test", PROGRAM, FILTROS, "x")

    assert len(cuentas) == 1
    # Misma forma que las cuentas de solana-py, para que el parseo de los
    # clientes de pump.py sirva igual venga de donde venga.
    assert str(cuentas[0].pubkey) == "Pool1111"
    assert cuentas[0].account.data == b"datos-del-pool"
    # Y se mandó con el método y los filtros correctos.
    assert len(hechas) == 1
    assert hechas[0]["method"] == "getProgramAccountsV2"
    assert hechas[0]["params"][1]["filters"] == [{"memcmp": {"offset": 43, "bytes": FILTROS[0].bytes}}]


async def test_la_v2_recorre_todas_las_paginas_aunque_vengan_vacias(fake_post):
    """Comprobado contra Helius: la paginación va sobre el conjunto CRUDO
    de cuentas del programa, así que la primera página puede volver vacía
    y el pool aparecer en la segunda. Cortar en la primera página vacía
    daría un 'no hay pool' falso."""
    hechas = fake_post([
        pagina([], pagination_key="sigue-1"),
        pagina([("Pool2222", b"xx")], pagination_key="sigue-2"),
        pagina([], pagination_key=None),
    ])
    client = FakeClient(RespuestaDeError(MENSAJE_HELIUS))

    cuentas = await _get_program_accounts(client, "http://rpc.test", PROGRAM, FILTROS, "x")

    assert [str(c.pubkey) for c in cuentas] == ["Pool2222"]
    assert len(hechas) == 3
    assert hechas[1]["params"][1]["paginationKey"] == "sigue-1"
    assert hechas[2]["params"][1]["paginationKey"] == "sigue-2"


async def test_un_barrido_truncado_lanza_en_vez_de_devolver_resultados_parciales(
        fake_post, monkeypatch):
    """Clave para no inventar un 'no hay pool': el llamador interpreta la
    lista vacía como `pool_confirmed_absent=True`, o sea una CONFIRMACIÓN
    de que el mint no tiene pool. Con la paginación a medias eso sería
    mentira, así que tiene que fallar explícitamente."""
    monkeypatch.setattr(pump_module, "_GPA_V2_MAX_PAGES", 3)
    fake_post([pagina([], pagination_key=f"sigue-{i}") for i in range(10)])
    client = FakeClient(RespuestaDeError(MENSAJE_HELIUS))

    with pytest.raises(RpcErrorResponse, match="no terminó de paginar"):
        await _get_program_accounts(client, "http://rpc.test", PROGRAM, FILTROS, "x")


async def test_otros_errores_del_rpc_no_disparan_la_v2(fake_post):
    """La V2 solo se intenta cuando el nodo la está pidiendo. Un error
    distinto (clave inválida, nodo caído) tiene que propagarse con su
    mensaje, no convertirse en un segundo intento a ciegas."""
    hechas = fake_post([])
    client = FakeClient(RespuestaDeError("Invalid API key"))

    with pytest.raises(RpcErrorResponse, match="Invalid API key"):
        await _get_program_accounts(client, "http://rpc.test", PROGRAM, FILTROS, "x")
    assert hechas == []


async def test_la_v2_propaga_su_propio_error_del_rpc(fake_post):
    fake_post([{"error": {"code": -32600, "message": "Pagination limit too large"}}])
    client = FakeClient(RespuestaDeError(MENSAJE_HELIUS))

    with pytest.raises(RpcErrorResponse, match="Pagination limit too large"):
        await _get_program_accounts(client, "http://rpc.test", PROGRAM, FILTROS, "x")
