"""
Tests del gestor de posiciones (manager.py), de punta a punta con bots
reales sobre el feed compartido (websocket falso) y un executor espía.
"""
import asyncio

import pytest

from pepump.dashboard import MultiPositionPanel
from pepump.feed import SharedTradeFeed
from pepump.manager import PositionManager

from .conftest import SpyExecutor, make_config
from .test_shared_feed import Conexiones, FakeClient

# Pubkeys válidas (validate_mint las exige).
A = "So11111111111111111111111111111111111111112"
B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def make_manager(**cfg_overrides):
    conns = Conexiones()
    feed = SharedTradeFeed(FakeClient(), connect=conns)
    executors = {}

    def executor_factory(cfg):
        executors[cfg.mint] = SpyExecutor()
        return executors[cfg.mint]

    manager = PositionManager(base_config=make_config(status_interval_seconds=999, **cfg_overrides),
                              client=None, feed=feed, executor_factory=executor_factory)
    return manager, conns, executors


async def _hasta(cond, timeout=2.0):
    async def _loop():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(_loop(), timeout)


async def _abrir_y_comprar(manager, conns, mint, price=1.0, **overrides):
    managed = manager.open(mint, **overrides)
    await _hasta(lambda: conns.sockets and mint in manager.feed.active_mints)
    conns.sockets[0].push({"mint": mint, "price": price})
    await _hasta(lambda: managed.bot.position is not None)
    return managed


async def test_dos_posiciones_un_websocket_y_close_all_vende_las_dos():
    manager, conns, executors = make_manager()
    pa = await _abrir_y_comprar(manager, conns, A)
    pb = await _abrir_y_comprar(manager, conns, B)
    assert len(conns.sockets) == 1

    manager.close_all("fin del test")
    await asyncio.wait_for(manager.wait_all(), 5)

    assert executors[A].sell_calls == 1 and executors[B].sell_calls == 1
    assert executors[A].last_sell_reason == "cierre manual (fin del test)"
    assert [s["phase"] for s in manager.snapshot()] == ["closed", "closed"]
    assert [s["id"] for s in manager.snapshot()] == [pa.id, pb.id]
    await manager.aclose()


async def test_no_deja_abrir_dos_veces_el_mismo_mint():
    manager, conns, executors = make_manager()
    primera = await _abrir_y_comprar(manager, conns, A)

    with pytest.raises(ValueError, match="Ya hay una posición activa"):
        manager.open(A)

    manager.close(primera.id, "test")
    await asyncio.wait_for(primera.task, 5)
    # Ya cerrada y vendida: se puede volver a abrir.
    segunda = manager.open(A)
    assert segunda.id != primera.id
    manager.close_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()


async def test_mint_ocupado_si_termino_con_la_posicion_abierta():
    """Si la venta de cierre falló, los tokens siguen en la wallet: ese
    mint sigue ocupado aunque el bot ya no corra."""
    manager, conns, executors = make_manager()
    pa = await _abrir_y_comprar(manager, conns, A)
    executors[A].fail_sell_times = 99
    manager.close(A, "test")
    await asyncio.wait_for(pa.task, 5)

    assert pa.active
    assert manager.snapshot()[0]["phase"] == "open"
    with pytest.raises(ValueError, match="Ya hay una posición activa"):
        manager.open(A)
    await manager.aclose()


async def test_ajustes_por_posicion():
    manager, conns, _ = make_manager()
    managed = manager.open(A, buy_sol=0.2, trailing_pct=7.5)
    assert managed.config.buy_sol == 0.2
    assert managed.config.trailing_pct == 7.5
    assert manager.base_config.buy_sol != 0.2  # la base no se toca
    assert managed.bot.snapshot()["settings"]["trailing_pct"] == 7.5

    with pytest.raises(ValueError, match="No se puede ajustar"):
        manager.open(B, live=True)
    with pytest.raises(ValueError, match="trailing_pct"):
        manager.open(B, trailing_pct=150)
    with pytest.raises(ValueError, match="no es una dirección de mint"):
        manager.open("no-es-un-mint")

    manager.close_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()


async def test_close_por_mint_y_close_all_bloquea_nuevas():
    manager, conns, executors = make_manager()
    pa = await _abrir_y_comprar(manager, conns, A)
    pb = await _abrir_y_comprar(manager, conns, B)

    manager.close(B, "solo B")
    await asyncio.wait_for(pb.task, 5)
    assert executors[B].sell_calls == 1
    assert not pa.task.done()

    manager.close_all("fin")
    with pytest.raises(ValueError, match="cerrando todas"):
        manager.open(B)
    await asyncio.wait_for(manager.wait_all(), 5)
    with pytest.raises(ValueError, match="No existe"):
        manager.close(99)
    await manager.aclose()


class FakeStatusPanel:
    def __init__(self):
        self.fixed, self.status = [], ""

    def set_all(self, fixed, status):
        self.fixed, self.status = list(fixed), status


async def test_panel_con_una_posicion_es_el_de_siempre_y_con_varias_una_fila_cada_una():
    manager, conns, _ = make_manager()
    fake = FakeStatusPanel()
    multi = MultiPositionPanel(fake, manager.snapshot, live=False)
    manager.panel_factory = multi.for_position

    pa = await _abrir_y_comprar(manager, conns, A)
    # Una sola: el bloque fijo del bot, tal cual.
    assert any(f"Mint {A}" in ln for ln in fake.fixed)

    await _abrir_y_comprar(manager, conns, B)
    multi.refresh()
    assert "2 posiciones" in fake.fixed[0]
    assert fake.fixed[1].startswith(f" #{pa.id} ")
    assert "abierta" in fake.fixed[1]
    assert fake.status == ""

    manager.close_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    multi.refresh()
    assert all("cerrada" in ln for ln in fake.fixed[1:])
    await manager.aclose()


async def test_closable_y_close_all_sin_bloquear():
    manager, conns, executors = make_manager()
    pa = await _abrir_y_comprar(manager, conns, A)
    assert pa.closable

    assert manager.close_all("desde la web", block_new=False) == 1
    assert not pa.closable  # ya pedido
    # Un segundo "cerrar todas" no vuelve a pedir nada.
    assert manager.close_all("otra vez", block_new=False) == 0
    await asyncio.wait_for(pa.task, 5)
    assert not pa.closable  # terminó
    assert executors[A].sell_calls == 1

    # El servicio sigue aceptando posiciones nuevas.
    pb = manager.open(B)
    manager.close_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    assert pb.task.done()
    await manager.aclose()
