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


async def test_open_guarda_el_coste_de_la_vista_previa_para_la_compra():
    manager, conns, _ = make_manager()
    managed = manager.open(A, expected_round_trip_pct=-0.2)
    assert managed.config.expected_round_trip_pct == -0.2
    assert manager.base_config.expected_round_trip_pct is None
    assert "expected_round_trip_pct" not in managed.bot.snapshot()["settings"]

    manager.close_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)


async def test_ajustes_por_posicion():
    manager, conns, _ = make_manager()
    managed = manager.open(A, buy_sol=0.2, trailing_pct=7.5)
    assert managed.config.buy_sol == 0.2
    assert managed.config.trailing_pct == 7.5
    assert manager.base_config.buy_sol != 0.2  # la base no se toca
    assert managed.bot.snapshot()["settings"]["trailing_pct"] == 7.5

    with pytest.raises(ValueError, match="No se puede ajustar"):
        manager.open(B, solana_rpc_url="http://otro-rpc")
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


# --------------------------------------------------------------------------- #
# Límites (paso 6: abrir desde la web)
# --------------------------------------------------------------------------- #

async def test_max_buy_sol_por_posicion():
    manager, conns, _ = make_manager(max_buy_sol=0.5)
    with pytest.raises(ValueError, match="max_buy_sol"):
        manager.open(A, buy_sol=10)
    assert manager.positions() == []
    manager.open(A, buy_sol=0.5)  # justo el máximo vale
    manager.close_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()


async def test_max_open_positions():
    manager, conns, _ = make_manager(max_open_positions=1)
    pa = manager.open(A)
    with pytest.raises(ValueError, match="max_open_positions"):
        manager.open(B)
    manager.close(pa.id, "fin")
    await asyncio.wait_for(pa.task, 5)
    manager.open(B)  # al cerrar la primera, hay hueco
    manager.close_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()


def test_defaults_para_el_formulario():
    manager, _, _ = make_manager(buy_sol=0.07, max_buy_sol=0.3, max_open_positions=4)
    d = manager.defaults()
    assert d["settings"]["buy_sol"] == 0.07
    assert d["max_buy_sol"] == 0.3 and d["max_open_positions"] == 4
    assert d["active_positions"] == 0 and d["accepting_new"] is True


async def test_preview(monkeypatch):
    import pepump.manager as manager_module

    class Curva:
        def __init__(self, rpc): pass
        async def fetch_price_or_status(self, mint):
            return (None, True, True) if mint == B else (2e-7, False, True)

    class Swap:
        def __init__(self, rpc): pass
        async def fetch_price_or_confirm_absent(self, mint):
            return 3e-6, False

    class Meta:
        async def fetch_name_symbol(self, mint):
            return ("Pepe Moon", "PEPEM")

    monkeypatch.setattr(manager_module, "PumpCurveOnChainClient", Curva)
    monkeypatch.setattr(manager_module, "PumpSwapOnChainClient", Swap)
    manager, _, _ = make_manager()
    manager.metadata_client = Meta()

    p = await manager.preview(A)
    assert (p["name"], p["symbol"], p["market"], p["price"]) == ("Pepe Moon", "PEPEM", "bondingcurve", 2e-7)
    p = await manager.preview(B)  # curva completada -> PumpSwap
    assert (p["market"], p["price"]) == ("pumpswap", 3e-6)
    with pytest.raises(ValueError):
        await manager.preview("no-es-un-mint")


async def test_preview_avisa_del_coste_de_entrar_y_salir(monkeypatch):
    """Caso real BTZK...WwGp por Jupiter: vender nada más comprar devolvía
    un ~12% menos (impuesto de transferencia del 3% en cada sentido,
    comisiones e impacto)."""
    import pepump.manager as manager_module

    class Curva:
        def __init__(self, rpc): pass
        async def fetch_price_or_status(self, mint):
            return None, False, False

    class Swap:
        def __init__(self, rpc): pass
        async def fetch_price_or_confirm_absent(self, mint):
            return None, True

    class Jupiter:
        async def buy_tokens(self, mint, sol, rpc_url, priority=None):
            self.sol = sol
            return 68788.8
        async def sell_price(self, mint, tokens, rpc_url, priority=None):
            return 0.88 * self.sol / tokens
        async def transfer_fee_pct(self, mint, rpc_url):
            return 3.0

    monkeypatch.setattr(manager_module, "PumpCurveOnChainClient", Curva)
    monkeypatch.setattr(manager_module, "PumpSwapOnChainClient", Swap)
    manager, _, _ = make_manager(buy_sol=0.05)
    manager.jupiter = Jupiter()

    p = await manager.preview(A)
    assert p["market"] == "jupiter" and p["buy_sol"] == 0.05
    assert p["round_trip_pct"] == pytest.approx(-12.0)
    assert p["transfer_fee_pct"] == 3.0
    # Con el importe del formulario, no el de la config.
    p = await manager.preview(A, buy_sol=0.2)
    assert manager.jupiter.sol == 0.2 and p["buy_sol"] == 0.2



async def test_modo_por_posicion():
    """Real o simulada se elige al abrir; el executor de cada posición
    recibe su modo."""
    manager, conns, executors = make_manager()  # general.live = False
    real = manager.open(A, live=True)
    simulada = manager.open(B)
    assert real.config.live is True and simulada.config.live is False
    assert real.bot.snapshot()["live"] is True
    assert manager.defaults()["settings"]["live"] is False
    manager.close_all("fin")
    await asyncio.wait_for(manager.wait_all(), 5)
    await manager.aclose()


class _ClienteConSaldo:
    wallet_address = None

    def __init__(self, sol):
        self.sol = sol
        self.consultas = 0

    async def get_sol_balance(self, rpc_url, address=None):
        self.consultas += 1
        return self.sol


async def test_wallet_info_y_check_funds():
    manager, _, _ = make_manager(wallet_address="So11111111111111111111111111111111111111112")
    manager.client = _ClienteConSaldo(0.03)
    info = await manager.wallet_info()
    assert info["sol"] == 0.03 and info["source"] == "config"
    await manager.wallet_info()
    assert manager.client.consultas == 1  # caché
    with pytest.raises(ValueError, match="Saldo insuficiente"):
        await manager.check_funds(0.05, 0.00001)
    await manager.check_funds(0.02, 0.00001)  # 0.02 + fee + margen cabe en 0.03
    await manager.aclose()


async def test_sin_wallet_conocida_no_bloquea():
    manager, _, _ = make_manager()
    manager.client = _ClienteConSaldo(None)
    assert (await manager.wallet_info())["address"] is None
    await manager.check_funds(100, 0.1)  # no se sabe el saldo: no bloquea
    await manager.aclose()
