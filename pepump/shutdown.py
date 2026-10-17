"""
Manejadores de señales del proceso (Ctrl+C, SIGTERM, Ctrl+Pausa) para el
cierre ordenado.

Antes vivían dentro de TrailingTakeProfitBot, pero las señales son del
PROCESO, no de una posición: con varios bots en el mismo proceso cada uno
pisaría los manejadores del anterior, y un bot controlado desde fuera (la
web) no tiene por qué tocar señales. Ahora el bot solo expone `close()`,
y quien lo arranca -run.py, o más adelante el gestor de posiciones-
decide qué hacer con cada señal.
"""
import asyncio
import logging
import signal
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# Señales que disparan el cierre ordenado. SIGBREAK (Ctrl+Pausa) solo
# existe en Windows, así que se filtra con getattr: en Unix no está y
# la tupla se queda con SIGINT/SIGTERM.
SHUTDOWN_SIGNALS = tuple(
    sig for sig in (getattr(signal, "SIGINT", None),
                    getattr(signal, "SIGTERM", None),
                    getattr(signal, "SIGBREAK", None))
    if sig is not None
)


def install_shutdown_handlers(loop, on_signal: Callable[[str], None]) -> List[Callable[[], None]]:
    """Instala los manejadores de cierre ordenado -cada señal llama a
    `on_signal(nombre_de_la_señal)` DENTRO del loop de eventos- y devuelve
    la lista de funciones que los desinstalan.

    Hay DOS caminos porque no existe uno que sirva en los dos
    sistemas operativos:

      1. `loop.add_signal_handler()` (Unix): el preferido. La señal
         nunca pasa por un manejador de Python, así que no hay
         ninguna ventana en la que llegue "por afuera" de la
         corrutina en ejecución.

      2. `signal.signal()` (Windows): add_signal_handler() es solo de
         Unix y en Windows lanza NotImplementedError. BUGFIX: sin este
         fallback no se instalaba NADA en Windows, así que el cierre
         ordenado no se pedía nunca: Ctrl+C subía como
         KeyboardInterrupt, run.py lo atrapaba y el proceso terminaba
         SIN VENDER la posición abierta -justo lo que el cierre
         ordenado existe para evitar, y con SOL real de por medio-.
         El manejador no llama a `on_signal` directamente (correría
         fuera del loop de eventos, donde Event.set() no despierta a
         nadie): hace `loop.call_soon_threadsafe`, que además despierta
         al ProactorEventLoop -desde Python 3.8 las señales escriben en
         el socket interno del loop, así que se atiende enseguida y no
         al llegar el próximo trade del websocket.

    OJO (Windows): SIGTERM se puede registrar pero el sistema no lo
    entrega nunca; `taskkill /F` mata el proceso en seco, sin darle
    oportunidad de vender. Ahí el cierre ordenado es Ctrl+C o
    Ctrl+Pausa."""
    restores = []
    for sig in SHUTDOWN_SIGNALS:
        restore = _install_one(loop, sig, on_signal)
        if restore is not None:
            restores.append(restore)
    if not restores:
        logger.warning("No se pudo instalar ningún manejador de señales en este sistema: el "
                       "cierre ordenado NO va a funcionar y una posición abierta puede quedar "
                       "SIN VENDER al salir. Ciérrala a mano si hace falta.")
    return restores


def _install_one(loop, sig, on_signal: Callable[[str], None]) -> Optional[Callable[[], None]]:
    """Instala el manejador de UNA señal por el mejor camino
    disponible (ver install_shutdown_handlers). Devuelve la función
    que lo desinstala, o None si no se pudo instalar por ninguno de
    los dos caminos."""
    try:
        loop.add_signal_handler(sig, on_signal, sig.name)
    except (NotImplementedError, RuntimeError):
        pass  # Windows, o no estamos en el hilo principal -> camino 2.
    else:
        def _quitar_de_asyncio() -> None:
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass
        return _quitar_de_asyncio

    sig_name = sig.name

    def _manejador(_signum, _frame) -> None:
        try:
            loop.call_soon_threadsafe(on_signal, sig_name)
        except RuntimeError:
            # El loop ya se cerró (señal justo durante el apagado):
            # no hay nada que coordinar, y propagar desde un
            # manejador de señales solo ensuciaría la salida.
            pass

    try:
        anterior = signal.signal(sig, _manejador)
    except (ValueError, OSError, RuntimeError) as e:
        # ValueError: no estamos en el hilo principal, o esta señal no
        # existe en este sistema. El aviso global (si no se pudo
        # instalar NINGUNA) lo da install_shutdown_handlers.
        logger.debug(f"No se pudo instalar manejador para {sig_name} en este sistema: {e}")
        return None

    logger.debug(f"Manejador de {sig_name} instalado vía signal.signal "
                 f"(add_signal_handler no está disponible en este sistema).")

    def _restaurar_anterior() -> None:
        # signal.signal() devuelve None cuando el manejador previo no
        # venía de Python: en ese caso no hay nada que restaurar, y
        # pasarle None sería un TypeError.
        if anterior is None:
            return
        try:
            signal.signal(sig, anterior)
        except Exception:
            pass
    return _restaurar_anterior


async def run_with_shutdown_signals_for(coro, on_signal: Callable[[str], None]) -> None:
    """Corre `coro` con las señales de cierre enganchadas a `on_signal`
    (se desinstalan siempre al terminar). Es lo que usa run.py con el
    gestor de posiciones: Ctrl+C -> cerrar todas."""
    loop = asyncio.get_running_loop()
    quitar = install_shutdown_handlers(loop, on_signal)
    try:
        await coro
    finally:
        for q in quitar:
            q()


async def run_with_shutdown_signals(bot) -> None:
    """Corre `bot.run()` con Ctrl+C/SIGTERM/Ctrl+Pausa enganchados a
    `bot.close()`: es lo que hacía run() por su cuenta antes de sacar las
    señales del bot. Los manejadores se instalan ANTES de conectar (una
    señal durante la conexión o la espera de entrada también corta
    ordenadamente) y se desinstalan siempre al terminar."""
    loop = asyncio.get_running_loop()
    quitar = install_shutdown_handlers(loop, lambda sig_name: bot.close(f"{sig_name} recibido"))
    try:
        await bot.run()
    finally:
        for q in quitar:
            q()
