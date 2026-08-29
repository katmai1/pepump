"""
Fixtures y dobles de prueba (fakes/spies) compartidos por toda la suite.

Los dobles de PumpPortalClient/TradeExecutor implementan a mano la misma
interfaz que usa TrailingTakeProfitBot (connect_trade_stream,
iter_trade_events, extract_price / buy, sell), para poder testear la
orquestación de bot.py sin pegarle a la red real (websocket de PumpPortal,
Lightning API, RPC de Solana).
"""
import asyncio

import pytest

from pepump.config import AppConfig
from pepump.executor import Position


def make_config(**overrides) -> AppConfig:
    """AppConfig real (no un mock) con overrides puntuales, para que los
    tests de bot.py usen exactamente los mismos defaults/campos que la
    app real y no se desincronicen si se agrega un campo nuevo."""
    cfg = AppConfig(mint="TestMint1111111111111111111111111111111111", api_key="dummy-key")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class FakeWebSocket:
    """Doble mínimo de una conexión websocket ya abierta."""

    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self.closed = True


class FakeTradeStreamClient:
    """
    Doble de PumpPortalClient enfocado en el feed de trades
    (connect_trade_stream / iter_trade_events / extract_price).

    `events_by_connection` es una lista de "tandas" de eventos: cada
    elemento es la lista de eventos que se van a entregar en la conexión
    N-ésima (permite simular una reconexión: la tanda 0 se corta y la
    tanda 1 es lo que llega después de reconectar).

    `fail_connect_times` permite simular que las primeras N llamadas a
    connect_trade_stream fallan (para testear el manejo de errores de
    conexión).
    """

    DATA_WS_URL = "wss://fake.pumpportal.test/api/data"

    def __init__(self, events_by_connection=None, fail_connect_times: int = 0,
                 connect_exc: Exception = None):
        self.events_by_connection = events_by_connection or [[]]
        self.fail_connect_times = fail_connect_times
        self.connect_exc = connect_exc or ConnectionRefusedError("conexión rechazada (fake)")
        self.connect_calls = 0
        self.sockets = []

    async def connect_trade_stream(self, mint: str):
        self.connect_calls += 1
        if self.connect_calls <= self.fail_connect_times:
            raise self.connect_exc
        ws = FakeWebSocket()
        self.sockets.append(ws)
        return ws

    async def iter_trade_events(self, ws):
        idx = self.sockets.index(ws)
        batch = self.events_by_connection[idx] if idx < len(self.events_by_connection) else []
        for event in batch:
            yield event
        # Al agotar la tanda, el generador simplemente termina
        # (StopAsyncIteration), igual que un cierre "limpio" del socket.

    @staticmethod
    def extract_price(event: dict):
        return event.get("price")


class SpyExecutor:
    """
    Doble de TradeExecutor que registra cuántas veces se llamó a sell()
    (clave para los tests de la carrera de doble venta) y puede simular
    latencia de red (para forzar que dos ventas puedan solaparse si el
    lock no las serializara) y/o una falla puntual de la Lightning API.
    """

    def __init__(self, sell_delay: float = 0.0, fail_sell_times: int = 0,
                 fail_exc: Exception = None):
        self.sell_delay = sell_delay
        self.fail_sell_times = fail_sell_times
        self.fail_exc = fail_exc or RuntimeError("Lightning API rechazó la orden (fake)")
        self.sell_calls = 0
        self.buy_calls = 0

    async def buy(self, mint: str, price: float) -> Position:
        self.buy_calls += 1
        return Position(mint=mint, entry_price=price, sol_amount=0.05, token_amount=0.05 / price)

    async def sell(self, position: Position, price: float, reason: str) -> None:
        self.sell_calls += 1
        if self.sell_delay:
            await asyncio.sleep(self.sell_delay)
        if self.sell_calls <= self.fail_sell_times:
            raise self.fail_exc
        position.closed = True


@pytest.fixture
def open_position():
    return Position(mint="TestMint1111111111111111111111111111111111",
                     entry_price=1.0, sol_amount=0.05, token_amount=0.05)
