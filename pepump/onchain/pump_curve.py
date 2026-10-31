"""Precio de un mint que sigue en la bonding curve de pump.fun."""
import logging
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

from pepump.onchain.programs import PUMPFUN_PROGRAM_ID
from pepump.onchain.rpc import rpc_value

logger = logging.getLogger(__name__)


class PumpCurveOnChainClient:
    """
    Fallback de precio para mints que TODAVÍA están en la bonding curve
    de pump.fun (no migraron a PumpSwap) -para el caso en que
    subscribeTokenTrade da el ack de suscripción pero no entrega NINGÚN
    trade real (ver bot.py: mismo síntoma que dispara
    PumpSwapOnChainClient, pero aquí ya confirmado por
    `PumpSwapOnChainClient.fetch_price_or_confirm_absent` que el mint
    sigue en bonding curve -pool_confirmed_absent=True-, así que no
    tiene sentido seguir esperando a ciegas un feed que puede no estar
    entregando nada por otro motivo).

    Lee la cuenta de la bonding curve DIRECTO de Solana vía RPC -la
    misma cuenta contra la que se ejecutaría el trade real, y la misma
    fuente que usa PumpPortal para calcular vSolInBondingCurve /
    vTokensInBondingCurve- así que no mete ningún desfase de una
    fuente externa.

    Layout de la cuenta (estable desde el lanzamiento del programa; el
    equipo de pump.fun documentó públicamente que la cuenta CRECIÓ para
    sumar campos nuevos -ej. cashback- pero los offsets de los campos
    viejos, incluidos los que usamos aquí, no cambiaron -ver
    pump-public-docs/PUMP_PROGRAM_README.md, que recomienda no depender
    del tamaño de la cuenta sino del discriminador):

      offset 0  (8 bytes): discriminador Anchor
      offset 8  (8 bytes): virtualTokenReserves (u64, little-endian, 6 decimales)
      offset 16 (8 bytes): virtualSolReserves (u64, little-endian, lamports)
      offset 24 (8 bytes): realTokenReserves (u64, little-endian)
      offset 32 (8 bytes): realSolReserves (u64, little-endian)
      offset 40 (8 bytes): tokenTotalSupply (u64, little-endian)
      offset 48 (1 byte):  complete (bool) — True = la curva ya se
                            completó (migró o está migrando a PumpSwap);
                            a partir de ahí este fallback deja de ser
                            confiable y hay que pasar a
                            PumpSwapOnChainClient.

    OJO: esto es SOLO lectura de una cuenta pública -no arma ni firma
    ninguna transacción de compra/venta (eso lo sigue haciendo
    PumpPortal Lightning API)-, así que los cambios de layout de
    INSTRUCCIONES de trading que hubo en el programa de pump.fun
    durante 2026 (la cuenta bonding-curve-v2 agregada como cuenta extra
    en las instrucciones buy/sell) no afectan nada aquí: seguimos
    leyendo la cuenta bonding-curve original (v1), que es la que trae
    las reservas y no cambió de offsets.
    """

    PROGRAM_ID = PUMPFUN_PROGRAM_ID
    # pump.fun: SOL tiene 9 decimales (lamports), los tokens de pump.fun
    # siempre tienen 6 decimales -hace falta este ajuste porque virtualSolReserves
    # y virtualTokenReserves NO están en la misma escala entre sí (a
    # diferencia de vSolInBondingCurve/vTokensInBondingCurve, que
    # PumpPortal ya entrega convertidos a unidades humanas).
    _SOL_DECIMALS = 9
    _TOKEN_DECIMALS = 6
    _VIRTUAL_TOKEN_RESERVES_OFFSET = 8
    _VIRTUAL_SOL_RESERVES_OFFSET = 16
    _COMPLETE_OFFSET = 48
    _MIN_ACCOUNT_LEN = _COMPLETE_OFFSET + 1

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    def _bonding_curve_address(self, mint: str) -> Pubkey:
        pda, _bump = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(self.PROGRAM_ID),
        )
        return pda

    async def fetch_price_or_status(self, mint: str) -> tuple[Optional[float], bool, Optional[bool]]:
        """Devuelve (price, complete, exists):
          - exists=False: CONFIRMADO que no existe cuenta de bonding
            curve para este mint (la consulta a la cuenta respondió
            bien y vino vacía) -señal fuerte de que este mint nunca se
            lanzó en pump.fun (ej. un mint nativo de Raydium/Meteora/
            otro DEX), a diferencia de exists=None (ver abajo). price y
            complete no significan nada en este caso.
          - exists=None: la consulta on-chain en sí falló (red/RPC/
            parseo) -NO es una confirmación de nada sobre la cuenta,
            a diferencia de exists=False. El llamador no debería sacar
            ninguna conclusión sobre si el mint es o no de pump.fun a
            partir de este caso.
          - exists=True, complete=True: la curva ya completó. price
            puede venir de todos modos (últimas reservas antes de
            completar) pero el llamador NO debería seguir operando con este fallback -
            hay que pasar a PumpSwapOnChainClient.
          - exists=True, complete=False, price no-None: caso normal,
            precio válido calculado de las reservas virtuales.

        Nunca lanza ninguna excepción hacia arriba (mismo criterio que
        PumpSwapOnChainClient.fetch_price_or_confirm_absent): cualquier
        error de red/RPC/parseo devuelve (None, False, None) -exists=None,
        no False- para no confundir un fallo de RPC con una confirmación
        real de que la cuenta no existe."""
        try:
            pda = self._bonding_curve_address(mint)
            async with AsyncClient(self.rpc_url) as client:
                resp = await client.get_account_info(pda, encoding="base64")
                info = rpc_value(resp, f"la cuenta de bonding curve de {mint}")
                if info is None:
                    logger.debug(f"[On-chain bonding curve] No existe cuenta de bonding curve para "
                                 f"{mint} ({pda}).")
                    return None, False, False

                data = info.data
                if len(data) < self._MIN_ACCOUNT_LEN:
                    logger.warning(f"[On-chain bonding curve] Cuenta de {mint} más pequeña de lo "
                                    f"esperado ({len(data)} bytes, se esperaban al menos "
                                    f"{self._MIN_ACCOUNT_LEN}); no se puede leer con este layout.")
                    return None, False, True

                v_tok = int.from_bytes(
                    data[self._VIRTUAL_TOKEN_RESERVES_OFFSET:self._VIRTUAL_TOKEN_RESERVES_OFFSET + 8],
                    "little",
                )
                v_sol = int.from_bytes(
                    data[self._VIRTUAL_SOL_RESERVES_OFFSET:self._VIRTUAL_SOL_RESERVES_OFFSET + 8],
                    "little",
                )
                complete = data[self._COMPLETE_OFFSET] != 0

                if v_tok <= 0:
                    logger.debug(f"[On-chain bonding curve] virtualTokenReserves=0 para {mint}; no se "
                                 f"puede calcular precio.")
                    return None, complete, True

                price = (v_sol / (10 ** self._SOL_DECIMALS)) / (v_tok / (10 ** self._TOKEN_DECIMALS))
                if price <= 0:
                    return None, complete, True

                logger.debug(f"[On-chain bonding curve] {pda} | reservas virtuales: "
                             f"{v_tok} tokens (raw) / {v_sol} lamports SOL | complete={complete}")
                return float(price), complete, True
        except Exception as e:
            logger.warning(f"[On-chain bonding curve] Falló la consulta on-chain para {mint}: {e}")
            return None, False, None
