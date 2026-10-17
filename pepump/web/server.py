"""
Panel web (solo lectura, por ahora): FastAPI dentro del MISMO proceso y
el mismo loop de asyncio que el gestor de posiciones, así lee el estado
directamente de él (manager.snapshot()) sin ninguna capa intermedia.

  GET  /                    la página (static/index.html)
  GET  /api/state           modo + todas las posiciones
  GET  /api/positions/{id}  una posición
  GET  /api/logs            últimos logs (?mint=...&after=<seq>&limit=...)
  WS   /ws                  estado cada segundo + logs en el momento

Seguridad: no hay NINGÚN endpoint que compre o venda. Aun así la página
muestra la actividad de tu wallet, así que:
  - por defecto solo escucha en 127.0.0.1,
  - se rechazan peticiones con un Host que no sea el esperado (protege
    contra DNS rebinding: una web maliciosa apuntando un dominio suyo a
    127.0.0.1),
  - y el WebSocket solo acepta conexiones cuyo Origin sea el propio
    panel: los WebSocket no tienen CORS, así que sin esta comprobación
    cualquier página abierta en tu navegador podría leer el estado.
"""
import asyncio
import contextlib
import ipaddress
import json
import logging
import socket
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pepump.manager import PositionManager
from pepump.web.logbuffer import LogBuffer

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# Cada cuánto manda el WebSocket el estado completo de las posiciones.
STATE_INTERVAL_SECONDS = 1.0
# Logs que recibe un navegador al conectarse (el resto, a medida que salen).
INITIAL_LOGS = 500


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def allowed_hosts_for(host: str) -> list:
    """Nombres de Host aceptados. Escuchando en todas las interfaces
    (0.0.0.0) no hay forma de saber con qué nombre o IP te van a llamar,
    así que ahí no se filtra (run.py avisa de que es peligroso)."""
    if host in ("0.0.0.0", "::", ""):
        return ["*"]
    return sorted({"127.0.0.1", "localhost", "::1", host})


def create_app(manager: PositionManager, logs: LogBuffer, *, live: bool,
               allowed_hosts: Optional[list] = None) -> FastAPI:
    app = FastAPI(title="PePump", docs_url=None, redoc_url=None, openapi_url=None)
    allowed = allowed_hosts or ["127.0.0.1", "localhost"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    def state() -> dict:
        return {"live": live, "server_time": time.time(), "positions": manager.snapshot()}

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    async def get_state():
        return state()

    @app.get("/api/positions/{pos_id}")
    async def get_position(pos_id: int):
        for snap in manager.snapshot():
            if snap["id"] == pos_id:
                return snap
        raise HTTPException(status_code=404, detail="No existe esa posición")

    @app.get("/api/logs")
    async def get_logs(mint: Optional[str] = None, after: int = 0, limit: int = 500):
        return logs.entries(mint=mint, after=after, limit=max(1, min(limit, 2000)))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if not _same_origin(websocket, allowed):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = logs.subscribe()
        try:
            await websocket.send_text(json.dumps({
                "type": "hello", "state": state(), "logs": logs.entries(limit=INITIAL_LOGS),
            }))
            next_state = time.monotonic() + STATE_INTERVAL_SECONDS
            while True:
                timeout = max(0.0, next_state - time.monotonic())
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({"type": "state", "state": state()}))
                    next_state = time.monotonic() + STATE_INTERVAL_SECONDS
                    continue
                batch = [entry]
                while not queue.empty() and len(batch) < 200:
                    batch.append(queue.get_nowait())
                await websocket.send_text(json.dumps({"type": "logs", "logs": batch}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            logs.unsubscribe(queue)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _same_origin(websocket: WebSocket, allowed: list) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        # Sin Origin no es un navegador (curl, scripts locales): no hay
        # riesgo de que sea una página ajena leyendo tu estado.
        return True
    origin_host = urlsplit(origin).netloc
    return origin_host == websocket.headers.get("host", "") and (
        "*" in allowed or urlsplit(origin).hostname in allowed)


class _EmbeddedServer(uvicorn.Server):
    """uvicorn sin sus manejadores de señales: Ctrl+C es del gestor (cerrar
    y vender todas las posiciones), no del servidor web. Si uvicorn los
    instalara, se comería la señal y pararía solo la web."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:  # uvicorn antiguos
        pass


def bind_socket(host: str, port: int) -> socket.socket:
    """Abre el puerto ANTES de arrancar nada: si está ocupado, el error
    sale enseguida y con un mensaje claro (uvicorn, si falla al abrirlo él,
    hace sys.exit en mitad del proceso, con posiciones ya abiertas)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.listen(128)
    sock.setblocking(False)
    return sock


class WebPanel:
    """El servidor web corriendo como una tarea más del loop."""

    def __init__(self, app: FastAPI, sock: socket.socket):
        config = uvicorn.Config(app, log_config=None, log_level="warning", access_log=False,
                                lifespan="off")
        self._server = _EmbeddedServer(config)
        self._sock = sock
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]), name="pepump-web")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        # Las conexiones WebSocket abiertas no terminan solas: sin esto,
        # uvicorn esperaría a que el navegador las cierre.
        self._server.force_exit = True
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(self._task, timeout=5)
        self._task = None

