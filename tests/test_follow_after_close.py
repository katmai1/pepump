"""Tras vender, el precio se sigue registrando un rato (on-chain, no por el
feed medido) para poder analizar después qué habría pasado con otros
ajustes. No toca la posición ni deja el mint ocupado."""
import asyncio

from pepump.bot import TrailingTakeProfitBot
from pepump.onchain import fallback as fallback_module
from tests.conftest import FakeTradeStreamClient, SpyExecutor, make_config
from tests.test_persistence import A, _hasta, _precio, make_manager


class _CurvaFalsa:
    """PumpCurveOnChainClient falso: devuelve los precios de la lista."""

    def __init__(self, precios):
        self.precios = list(precios)

    def __call__(self, rpc_url):
        return self

    async def fetch_price_or_status(self, mint):
        price = self.precios.pop(0) if self.precios else None
        return price, False, True


def _bot_vendido():
    from pepump.executor import Position
    cfg = make_config(onchain_poll_interval_seconds=0.001)
    bot = TrailingTakeProfitBot(FakeTradeStreamClient(), SpyExecutor(), cfg)
    bot.position = Position(mint=cfg.mint, entry_price=1.0, sol_amount=0.05, token_amount=0.05)
    bot.position.closed = True
    return bot


async def test_registra_precios_on_chain_tras_la_venta(monkeypatch):
    # Saltos > 3% para que el muestreo del gráfico no descarte ninguno.
    monkeypatch.setattr(fallback_module, "PumpCurveOnChainClient", _CurvaFalsa([1.0, 1.2, 0.9]))
    bot = _bot_vendido()
    guardados = []
    bot._price_cb = lambda ts, price: guardados.append(price)

    await asyncio.wait_for(bot.follow_after_close(0.2), 5)

    assert guardados == [1.0, 1.2, 0.9]
    assert [p for _, p in bot.price_history] == [1.0, 1.2, 0.9]
    assert bot.position.closed  # la posición no se toca
    assert bot.onchain.source is None  # ni la fuente que muestra el panel


async def test_un_fallo_de_rpc_no_corta_el_registro(monkeypatch):
    class _Rota(_CurvaFalsa):
        async def fetch_price_or_status(self, mint):
            if len(self.precios) == 2:
                self.precios.pop(0)
                raise RuntimeError("RPC caído")
            return await super().fetch_price_or_status(mint)

    monkeypatch.setattr(fallback_module, "PumpCurveOnChainClient", _Rota([1.0, None, 2.0]))
    bot = _bot_vendido()
    await asyncio.wait_for(bot.follow_after_close(0.2), 5)
    assert [p for _, p in bot.price_history] == [1.0, 2.0]


async def test_el_gestor_lo_lanza_tras_vender_sin_ocupar_el_mint(store, monkeypatch):
    monkeypatch.setattr(fallback_module, "PumpCurveOnChainClient", _CurvaFalsa([]))
    manager, conns, _ = make_manager(store, onchain_poll_interval_seconds=0.01)
    managed = manager.open(A)
    await _precio(manager, conns, A, 1.0)
    await _hasta(lambda: managed.bot.position is not None)
    manager.close(A, "test")
    await asyncio.wait_for(managed.task, 5)

    await _hasta(lambda: len(manager._followers) == 1)
    assert not managed.active  # el mint queda libre
    assert manager.active_count() == 0

    await manager.aclose()  # corta el registro al apagar
    assert not manager._followers


async def test_no_se_lanza_si_el_proceso_se_esta_apagando(store, monkeypatch):
    monkeypatch.setattr(fallback_module, "PumpCurveOnChainClient", _CurvaFalsa([]))
    manager, conns, _ = make_manager(store)
    managed = manager.open(A)
    await _precio(manager, conns, A, 1.0)
    await _hasta(lambda: managed.bot.position is not None)
    manager.close_all("Ctrl+C con --sell-on-exit")  # block_new=True: el proceso sale
    await asyncio.wait_for(managed.task, 5)
    await asyncio.sleep(0)

    assert not manager._followers
    await manager.aclose()
