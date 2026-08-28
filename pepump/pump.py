import requests
import websockets
import json
from typing import AsyncIterator, Optional


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

        Mientras el token sigue en la bonding curve de pump.fun, el evento
        trae las reservas virtuales (`vSolInBondingCurve`/
        `vTokensInBondingCurve`) y el precio sale de ahí, exacto.

        Si el token ya migró a PumpSwap (típico en tokens de bastante
        volumen), el evento deja de traer esas reservas; en ese caso se usa
        `marketCapSol` -que sí viene en todos los trades, migrados o no-
        junto con el supply estándar de pump.fun para derivar el precio.
        """
        v_sol = event.get("vSolInBondingCurve")
        v_tok = event.get("vTokensInBondingCurve")
        if v_sol is not None and v_tok is not None and v_tok:
            return v_sol / v_tok

        market_cap_sol = event.get("marketCapSol")
        if market_cap_sol is not None:
            try:
                return float(market_cap_sol) / cls.TOTAL_SUPPLY_TOKENS
            except (TypeError, ValueError):
                return None

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

