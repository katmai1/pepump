"""
Tests de la instalación de los manejadores de cierre ordenado
(TrailingTakeProfitBot._install_shutdown_handlers).

BUGFIX (Windows se quedaba sin cierre ordenado): antes solo se intentaba
`loop.add_signal_handler()`, que es exclusivo de Unix. En Windows lanza
NotImplementedError, el except se lo comía y NO quedaba instalado nada:
`_shutdown_requested` no se marcaba nunca, Ctrl+C subía como
KeyboardInterrupt y run.py terminaba el proceso SIN VENDER la posición
abierta. Ahora hay un fallback con `signal.signal()`.

Windows se simula pisando `add_signal_handler` para que lance
NotImplementedError igual que allí, en vez de depender del sistema donde
corran los tests.
"""
import asyncio
import signal

import pytest

from pepump.bot import TrailingTakeProfitBot

from .conftest import FakeTradeStreamClient, SpyExecutor, make_config


def make_bot():
    return TrailingTakeProfitBot(client=FakeTradeStreamClient(),
                                  executor=SpyExecutor(),
                                  config=make_config())


@pytest.fixture
async def sin_add_signal_handler(monkeypatch):
    """Simula Windows: `loop.add_signal_handler` lanza NotImplementedError,
    que es exactamente lo que hace allí (es una API solo de Unix)."""
    def _no_implementado(self, *a, **kw):
        raise NotImplementedError("add_signal_handler es solo de Unix (fake Windows)")
    monkeypatch.setattr(type(asyncio.get_running_loop()), "add_signal_handler",
                         _no_implementado)


@pytest.fixture
def restaurar_señales():
    """Devuelve los manejadores de proceso a como estaban, pase lo que
    pase: estos tests los pisan de verdad (no con un mock), y dejarlos
    cambiados rompería a pytest y a los tests que corran después."""
    previos = {}
    for sig in TrailingTakeProfitBot._SHUTDOWN_SIGNALS:
        previos[sig] = signal.getsignal(sig)
    yield
    for sig, handler in previos.items():
        if handler is not None:
            signal.signal(sig, handler)


async def test_usa_add_signal_handler_cuando_esta_disponible(monkeypatch):
    """Camino preferido: si add_signal_handler está (Unix), se usa ese y
    NO se recurre al fallback de signal.signal."""
    bot = make_bot()
    loop = asyncio.get_running_loop()
    enganchadas = []
    desenganchadas = []

    real_add = type(loop).add_signal_handler
    real_remove = type(loop).remove_signal_handler

    def _spy_add(self, sig, callback, *args):
        enganchadas.append((sig, callback, args))
        return real_add(self, sig, callback, *args)

    def _spy_remove(self, sig):
        desenganchadas.append(sig)
        return real_remove(self, sig)

    monkeypatch.setattr(type(loop), "add_signal_handler", _spy_add)
    monkeypatch.setattr(type(loop), "remove_signal_handler", _spy_remove)

    quitar = bot._install_shutdown_handlers(loop)
    try:
        assert len(quitar) == len(bot._SHUTDOWN_SIGNALS)
        assert [sig for sig, _cb, _a in enganchadas] == list(bot._SHUTDOWN_SIGNALS)
        # Cada señal queda atada a _request_shutdown con su propio nombre.
        for sig, callback, args in enganchadas:
            assert callback == bot._request_shutdown
            assert args == (sig.name,)
    finally:
        for q in quitar:
            q()

    # Y se desinstalan por el mismo camino (remove_signal_handler), no por
    # el de signal.signal: es la prueba de que se usó el camino de asyncio.
    assert desenganchadas == list(bot._SHUTDOWN_SIGNALS)


async def test_fallback_a_signal_signal_cuando_no_hay_add_signal_handler(
        sin_add_signal_handler, restaurar_señales):
    """El fix: en Windows se instala por signal.signal en vez de quedarse
    sin ningún manejador."""
    bot = make_bot()
    loop = asyncio.get_running_loop()

    quitar = bot._install_shutdown_handlers(loop)
    try:
        assert len(quitar) == len(bot._SHUTDOWN_SIGNALS)
        for sig in bot._SHUTDOWN_SIGNALS:
            assert callable(signal.getsignal(sig))
            assert signal.getsignal(sig) is not signal.default_int_handler
    finally:
        for q in quitar:
            q()


async def test_el_fallback_marca_el_evento_de_apagado(
        sin_add_signal_handler, restaurar_señales):
    """Lo que de verdad importa: cuando llega la señal, el manejador de
    Windows termina marcando `_shutdown_requested` -que es lo que hace
    que run() venda la posición antes de salir."""
    bot = make_bot()
    loop = asyncio.get_running_loop()

    quitar = bot._install_shutdown_handlers(loop)
    try:
        assert not bot._shutdown_requested.is_set()
        # Se invoca el manejador como lo invocaría el sistema al recibir
        # la señal (mismo hilo principal, con signum/frame).
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        # El manejador usa call_soon_threadsafe: el Event se marca en el
        # próximo ciclo del loop, no de forma sincrónica.
        await asyncio.wait_for(bot._shutdown_requested.wait(), timeout=1)
    finally:
        for q in quitar:
            q()


async def test_restaurar_devuelve_el_manejador_previo(
        sin_add_signal_handler, restaurar_señales):
    """El finally de run() tiene que dejar el proceso como estaba: si no,
    un segundo bot en el mismo proceso (o el propio intérprete al salir)
    se queda con un manejador que apunta a un loop ya cerrado."""
    bot = make_bot()
    loop = asyncio.get_running_loop()

    def _manejador_propio(_signum, _frame):
        pass
    signal.signal(signal.SIGINT, _manejador_propio)

    quitar = bot._install_shutdown_handlers(loop)
    assert signal.getsignal(signal.SIGINT) is not _manejador_propio
    for q in quitar:
        q()
    assert signal.getsignal(signal.SIGINT) is _manejador_propio


async def test_avisa_si_no_se_pudo_instalar_ninguno(monkeypatch, caplog):
    """Si ningún camino sirve, el usuario tiene que enterarse de que una
    posición abierta puede quedar sin vender -no quedarse en silencio."""
    bot = make_bot()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(bot, "_install_one_shutdown_handler", lambda loop, sig: None)

    with caplog.at_level("WARNING"):
        quitar = bot._install_shutdown_handlers(loop)

    assert quitar == []
    assert any("SIN VENDER" in rec.message for rec in caplog.records)


async def test_run_vende_la_posicion_con_el_fallback_de_windows(
        sin_add_signal_handler, restaurar_señales):
    """End to end del bug: con add_signal_handler no disponible (Windows),
    una señal durante una posición abierta tiene que cerrarla igual.
    Antes del fix, aquí no se instalaba nada, `_shutdown_requested` no se
    marcaba y run() se quedaba esperando con la posición abierta."""
    cfg = make_config(status_interval_seconds=999)
    # Solo el trade inicial: el stream queda abierto y en silencio, así
    # que lo único que puede terminar run() es la señal.
    client = FakeTradeStreamClient(events_by_connection=[[{"price": 1.0}]])
    executor = SpyExecutor()
    bot = TrailingTakeProfitBot(client=client, executor=executor, config=cfg)

    async def mandar_señal_tras_comprar():
        while bot.position is None:
            await asyncio.sleep(0.005)
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

    asyncio.create_task(mandar_señal_tras_comprar())
    await asyncio.wait_for(bot.run(), timeout=5)

    assert executor.buy_calls == 1
    assert executor.sell_calls == 1
    assert bot.position.closed is True
