"""
Fixtures y dobles de prueba (fakes/spies) compartidos por toda la suite.

Los dobles de PumpPortalClient/TradeExecutor implementan a mano la misma
interfaz que usa TrailingTakeProfitBot (connect_trade_stream,
iter_trade_events, extract_price / buy, sell), para poder testear la
orquestación de bot.py sin tocar la red real (websocket de PumpPortal,
Lightning API, RPC de Solana).
"""
import asyncio
import time
from typing import Optional

import pytest

from pepump.config import AppConfig
from pepump.executor import Position


def make_config(**overrides) -> AppConfig:
    """AppConfig real (no un mock) con overrides puntuales, para que los
    tests de bot.py usen exactamente los mismos defaults/campos que la
    app real y no se desincronicen si se agrega un campo nuevo."""
    # RPC local que no existe: la conexión se rechaza al instante. Los tests
    # nunca deben llegar a mainnet (lentos, no deterministas, y gastan cupo
    # de un RPC real); los que necesitan respuestas on-chain usan dobles.
    cfg = AppConfig(mint="TestMint1111111111111111111111111111111111", api_key="dummy-key",
                    solana_rpc_url="http://127.0.0.1:9")
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
                 connect_exc: Optional[Exception] = None):
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
                 fail_exc: Optional[Exception] = None):
        self.sell_delay = sell_delay
        self.fail_sell_times = fail_sell_times
        self.fail_exc = fail_exc or RuntimeError("Lightning API rechazó la orden (fake)")
        self.sell_calls = 0
        self.buy_calls = 0
        # Últimos pool_override recibidos (para que los tests puedan
        # verificar que bot.py fuerza pool="pump-amm" cuando corresponde).
        self.last_buy_pool_override = None
        self.last_sell_pool_override = None
        self.last_sell_reason = None
        # (slippage, priority_fee) de cada intento de venta, en orden.
        self.sell_params: list = []

    async def buy(self, mint: str, price: float, pool_override: Optional[str] = None) -> Position:
        self.buy_calls += 1
        self.last_buy_pool_override = pool_override
        return Position(mint=mint, entry_price=price, sol_amount=0.05, token_amount=0.05 / price)

    async def sell(self, position: Position, price: float, reason: str, pool_override: Optional[str] = None,
                   slippage: Optional[float] = None, priority_fee: Optional[float] = None) -> None:
        self.sell_calls += 1
        self.last_sell_pool_override = pool_override
        self.sell_params.append((slippage, priority_fee))
        self.last_sell_reason = reason
        if self.sell_delay:
            await asyncio.sleep(self.sell_delay)
        if self.sell_calls <= self.fail_sell_times:
            raise self.fail_exc
        # Lo mismo que rellena TradeExecutor.sell() al confirmar.
        position.closed_at = time.time()
        position.exit_price = price
        position.proceeds_sol = position.token_amount * price
        position.realized_pnl_sol = position.proceeds_sol - position.sol_amount
        position.realized_pnl_pct = position.realized_pnl_sol / position.sol_amount * 100.0
        position.close_reason = reason
        position.closed = True


class _RaydiumCpmmSinPools:
    """Doble de RaydiumCpmmOnChainClient y de los clientes de Meteora que
    siempre confirma 'no hay pool'."""

    def __init__(self, rpc_url):
        self.rpc_url = rpc_url

    async def fetch_price_or_confirm_absent(self, mint: str):
        return None, True

    async def fetch_price(self, mint: str):
        return None

    async def fetch_price_reserve_or_confirm_absent(self, mint: str):
        return None, None, True


@pytest.fixture(autouse=True)
def _reintentos_de_venta_sin_pausa(monkeypatch):
    """La pausa entre reintentos de venta (1 s, 2 s...) solo alargaría los
    tests; el que la comprueba la vuelve a poner."""
    from pepump.bot import TrailingTakeProfitBot
    monkeypatch.setattr(TrailingTakeProfitBot, "_SELL_RETRY_DELAY_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _sin_raydium_cpmm_real(monkeypatch):
    """Ningún test del bot le pega al RPC real de Raydium CPMM ni de
    Meteora (DLMM, DAMM v2) por accidente: por defecto 'no hay pool'. Los tests que
    necesitan otra cosa lo pisan con su propio monkeypatch."""
    from pepump.onchain import fallback as fallback_module
    monkeypatch.setattr(fallback_module, "RaydiumCpmmOnChainClient", _RaydiumCpmmSinPools)
    monkeypatch.setattr(fallback_module, "MeteoraDlmmOnChainClient", _RaydiumCpmmSinPools)
    monkeypatch.setattr(fallback_module, "MeteoraDammV2OnChainClient", _RaydiumCpmmSinPools)


@pytest.fixture
def open_position():
    return Position(mint="TestMint1111111111111111111111111111111111",
                     entry_price=1.0, sol_amount=0.05, token_amount=0.05)


@pytest.fixture
def store(tmp_path):
    """Base de datos de posiciones en un archivo temporal."""
    from pepump.store import PositionStore
    s = PositionStore(str(tmp_path / "estado.db"))
    yield s
    s.close()
