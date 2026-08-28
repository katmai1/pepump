import requests
import websockets
import json
from typing import AsyncIterator, Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey  # type: ignore
from pumpswapamm.pumpswapamm import fetch_pool_state
from pumpswapamm.fetch_reserves import fetch_pool_base_price

# Programa de PumpSwap en Solana (constante pública, no cambia).
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Mint del SOL "wrapped" (WSOL) — para confirmar que el pool que
# encontramos está denominado en SOL antes de usar su precio.
WSOL_MINT = "So11111111111111111111111111111111111111112"


class PumpPortalClient:
    """
    Encapsula toda la comunicación de red con PumpPortal. No conoce nada
    sobre estrategia de trading: solo sabe pedir datos y mandar órdenes
    a través de la Lightning Transaction API (PumpPortal firma y envía la
    transacción por su cuenta; nosotros solo necesitamos la API key).
    """

    DATA_WS_URL = "wss://pumpportal.fun/api/data"
    LIGHTNING_TRADE_URL = "https://pumpportal.fun/api/trade"

    # Supply estándar de un token creado en pump.fun (1.000.000.000 tokens),
    # usado para derivar el precio a partir de `marketCapSol` en los trades
    # que ya no traen las reservas de la bonding curve (token migrado a
    # PumpSwap). Ver extract_price.
    TOTAL_SUPPLY_TOKENS = 1_000_000_000

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    # ---- Feed de precios (SOLO PumpPortal, subscribeTokenTrade) ---------- #

    async def connect_trade_stream(self, mint: str):
        """
        Abre la conexión al websocket de datos de PumpPortal y hace el
        subscribe_trade (`subscribeTokenTrade`) al `mint` indicado, dejando
        la conexión abierta para que se pueda reutilizar tanto para
        conseguir el precio inicial (antes de comprar) como para el
        monitoreo posterior, sin reconectar ni volver a suscribirse.

        OJO: `subscribeTokenTrade` es un stream medido de PumpPortal (0.01
        SOL cada 10.000 eventos) y requiere una API key vinculada a una
        wallet con al menos 0.02 SOL, sin importar si el bot está en modo
        SIMULADO o REAL. Sin API key, la conexión y la suscripción NO dan
        error, pero tampoco entregan ningún trade: el bot se queda
        esperando para siempre (no hay otra fuente de precio de respaldo).
        """
        url = self.DATA_WS_URL
        if self.api_key:
            url = f"{url}?api-key={self.api_key}"
        else:
            print("⚠️  Sin pumpportal.api_key configurada: subscribeTokenTrade NO va a entregar "
                  "ningún trade (requiere API key + wallet con >= 0.02 SOL, aunque el bot esté en "
                  "modo SIMULADO). Como este bot usa SOLO PumpPortal para el precio, se va a quedar "
                  "esperando para siempre. Configurá pumpportal.api_key o PUMPPORTAL_API_KEY.")

        ws = await websockets.connect(url)
        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
        return ws

    @staticmethod
    async def iter_trade_events(ws) -> AsyncIterator[dict]:
        """Va entregando cada evento crudo (dict) que llega por una conexión
        ya abierta y suscripta (ver connect_trade_stream)."""
        async for raw_msg in ws:
            try:
                yield json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

    @classmethod
    def extract_price(cls, event: dict) -> Optional[float]:
        """
        Precio en SOL/token a partir de un evento de subscribeTokenTrade.
        Tres niveles, todos con datos que PumpPortal ya manda en el propio
        evento — sin recurrir a ninguna fuente externa (DexScreener, etc.)
        que podría desalinearse del precio real de ejecución y meter
        slippage entre lo que el bot "ve" y lo que realmente paga:

        1. Mientras el token sigue en la bonding curve de pump.fun, el
           evento trae las reservas virtuales (`vSolInBondingCurve`/
           `vTokensInBondingCurve`) y el precio sale de ahí, exacto.

        2. Si el token ya migró a PumpSwap/Raydium, esas reservas dejan de
           venir; en ese caso se usa `marketCapSol` -que sí viene en todos
           los trades, migrados o no- junto con el supply estándar de
           pump.fun (1.000.000.000 tokens) para derivar el precio.

        3. Si tampoco viene `marketCapSol` (algunos trades de pools ya
           migrados no lo incluyen), se cae al precio efectivo de ESE
           trade puntual: `solAmount / tokenAmount`, los montos reales que
           se intercambiaron en esa operación. Es el nivel menos preciso
           de los tres (es el precio de UN trade, no una cotización
           instantánea de reservas), pero sigue siendo 100% PumpPortal,
           en vivo, sin fuentes externas.
        """
        v_sol = event.get("vSolInBondingCurve")
        v_tok = event.get("vTokensInBondingCurve")
        if v_sol is not None and v_tok is not None and v_tok:
            return v_sol / v_tok

        market_cap_sol = event.get("marketCapSol")
        if market_cap_sol is not None:
            try:
                price = float(market_cap_sol) / cls.TOTAL_SUPPLY_TOKENS
                if price > 0:
                    return price
            except (TypeError, ValueError):
                pass

        sol_amount = event.get("solAmount")
        token_amount = event.get("tokenAmount")
        if sol_amount is not None and token_amount is not None:
            try:
                sol_amount = float(sol_amount)
                token_amount = float(token_amount)
                if token_amount > 0:
                    return sol_amount / token_amount
            except (TypeError, ValueError):
                pass

        return None

    # ---- Trading real (Lightning Transaction API) ------------------------- #

    def execute_lightning_trade(self, action: str, mint: str, amount, denominated_in_sol: bool,
                                 slippage: float, priority_fee: float, pool: str) -> dict:
        """
        Manda la orden a la Lightning API. PumpPortal arma, firma y envía la
        transacción del lado de ellos usando la wallet asociada a la API key.
        Devuelve el JSON de respuesta (incluye la firma de la tx o errores).
        """
        if not self.api_key:
            raise RuntimeError("Se necesita una API key de PumpPortal para operar con la Lightning API.")

        payload = {
            "action": action,  # "buy" o "sell"
            "mint": mint,
            "denominatedInSol": "true" if denominated_in_sol else "false",
            "amount": amount,
            "slippage": slippage,
            "priorityFee": priority_fee,
            "pool": pool,
        }

        resp = requests.post(
            f"{self.LIGHTNING_TRADE_URL}?api-key={self.api_key}",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Lightning API devolvió {resp.status_code}: {resp.text}")
        return resp.json()


class PumpSwapOnChainClient:
    """
    Fallback de precio ÚNICAMENTE para mints que ya migraron a PumpSwap
    (ver bot.py: se usa solo si subscribeTokenTrade confirma el ack pero
    no entrega NINGÚN trade dentro de `live_feed_timeout_seconds` — el
    síntoma real, confirmado a mano, de un mint que ya salió de la
    bonding curve). Lee las reservas del pool DIRECTO de Solana vía RPC
    -la misma cuenta contra la que se ejecutaría el trade real- así que
    no mete ningún desfasaje de una fuente externa tipo DexScreener.

    Usa la librería `pumpswapamm` (github.com/FLOCK4H/PumpSwapAMM) solo
    para parsear la cuenta del pool; el descubrimiento del pool a partir
    del mint lo hacemos nosotros con un getProgramAccounts + memcmp
    directo sobre el programa de PumpSwap, porque esa librería no trae
    una función para "encontrar el pool de este mint" (solo puede leer
    un pool si ya conocés su dirección, o derivarla si ya conocés el
    `creator`, que para un mint migrado automáticamente desde pump.fun
    no es el wallet que creó el token).

    OJO: pumpswapamm es de un solo mantenedor y no está auditada. Se usa
    acá solo para DECODIFICAR una cuenta pública de solo lectura (no
    firma ni manda transacciones), pero aun así es una dependencia
    externa nueva — tenelo en cuenta.
    """

    # Offset en bytes de `base_mint` dentro de la cuenta del pool,
    # verificado contra el struct real de pumpswapamm
    # (PumpSwapPoolStateNew/Old en pumpswapamm.py):
    #   8 (discriminador Anchor) + 1 (pool_bump) + 2 (index) + 32 (creator)
    _BASE_MINT_OFFSET = 43

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def fetch_price_for_migrated_mint(self, mint: str) -> Optional[float]:
        """Busca el pool de PumpSwap para `mint` y devuelve su precio
        actual en SOL/token leyendo las reservas on-chain. None si no
        encuentra el pool, si no está denominado en SOL, o si falla la
        lectura (red, RPC caído, etc.) — nunca tira excepción hacia
        arriba, para que el bot pueda seguir esperando el feed en vivo
        en vez de caerse por un problema de este fallback secundario."""
        async with AsyncClient(self.rpc_url) as client:
            try:
                pool_address = await self._find_pool_address(client, mint)
                if pool_address is None:
                    print(f"[On-chain PumpSwap] No se encontró ningún pool de PumpSwap para {mint}.")
                    return None

                pool_keys, _pool_type = await fetch_pool_state(pool_address, client)
                if pool_keys is None:
                    print("[On-chain PumpSwap] No se pudo leer/parsear la cuenta del pool.")
                    return None

                if pool_keys.get("quote_mint") != WSOL_MINT:
                    print(f"[On-chain PumpSwap] El pool de {mint} no está denominado en SOL "
                          f"(quote_mint={pool_keys.get('quote_mint')}); no lo puedo usar acá.")
                    return None

                result = await fetch_pool_base_price(pool_keys, client)
                if result is None:
                    print("[On-chain PumpSwap] No se pudieron leer las reservas del pool.")
                    return None

                price, base_balance, quote_balance = result
                if not base_balance or float(price) <= 0:
                    return None

                print(f"[On-chain PumpSwap] Pool {pool_address} | reservas: "
                      f"{base_balance} tokens / {quote_balance} SOL")
                return float(price)
            except Exception as e:
                print(f"[On-chain PumpSwap] Falló la consulta on-chain para {mint}: {e}")
                return None

    async def _find_pool_address(self, client: AsyncClient, mint: str) -> Optional[str]:
        """getProgramAccounts sobre el programa de PumpSwap, filtrando por
        `base_mint == mint` con un memcmp en el offset exacto del struct.
        Si hay varios pools para el mismo mint (raro, pero el struct
        soporta `index`), nos quedamos con el de mayor `lp_supply` (el
        pool "real" con liquidez, no uno vacío/de prueba)."""
        resp = await client.get_program_accounts(
            Pubkey.from_string(PUMPSWAP_PROGRAM_ID),
            encoding="base64",
            filters=[MemcmpOpts(offset=self._BASE_MINT_OFFSET, bytes=mint)],
        )
        accounts = resp.value
        if not accounts:
            return None
        if len(accounts) == 1:
            return str(accounts[0].pubkey)

        # Más de un pool para el mismo mint: nos quedamos con el de mayor
        # lp_supply comparando el account data crudo (evita otro round-trip
        # de RPC por cada candidato).
        best_pubkey = None
        best_lp_supply = -1
        for acc in accounts:
            try:
                pool_keys, _ = await fetch_pool_state(acc.pubkey, client)
                lp_supply = (pool_keys or {}).get("lp_supply", 0)
                if lp_supply > best_lp_supply:
                    best_lp_supply = lp_supply
                    best_pubkey = str(acc.pubkey)
            except Exception:
                continue
        return best_pubkey