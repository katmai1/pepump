"""
Panel web: FastAPI dentro del MISMO proceso y el mismo loop de asyncio
que el gestor de posiciones, así lee el estado y manda órdenes
directamente a él (manager.snapshot(), manager.close()) sin ninguna capa
intermedia.

  GET  /                              la página (static/index.html)
  GET  /api/session                   ¿pide contraseña? ¿ya entraste?
  POST /api/login                     {"password": "..."} -> cookie de sesión
  POST /api/logout
  GET  /api/state                     modo + todas las posiciones
  GET  /api/positions/{id}            una posición
  POST /api/positions/{id}/close      cerrar (vender) una posición
  POST /api/positions/close-all       cerrar todas
  GET  /api/logs                      últimos logs (?mint=...&after=<seq>&limit=...)
  GET  /api/history                   posiciones terminadas en ejecuciones anteriores
  WS   /ws                            estado cada segundo + logs en el momento

Seguridad:
  - Contraseña OPCIONAL (web_password en el .toml). Si está puesta, todo
    lo de /api y el WebSocket exige haber entrado con ella; si no, entra
    cualquiera que llegue al puerto (run.py avisa si además escucha en la
    red). La sesión es una cookie HttpOnly + SameSite=Strict que solo vive
    en memoria: al reiniciar el proceso hay que volver a entrar.
  - Por defecto solo escucha en 127.0.0.1.
  - Se rechazan peticiones con un Host que no sea el esperado (protege
    contra DNS rebinding: una web maliciosa apuntando un dominio suyo a
    tu IP).
  - Las órdenes (POST) exigen la cabecera X-PePump y, si el navegador
    manda Origin, que sea el propio panel. Una página ajena abierta en tu
    navegador no puede mandar esa cabecera sin permiso del servidor
    (CORS), así que no puede cerrarte posiciones aunque no haya
    contraseña. Lo mismo para el WebSocket, que no tiene CORS: se
    comprueba el Origin a mano.
"""
import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
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
SESSION_COOKIE = "pepump_session"
CSRF_HEADER = "x-pepump"
# Cierre del WebSocket por falta de sesión: el navegador vuelve a la
# pantalla de contraseña en vez de reintentar.
WS_CLOSE_UNAUTHORIZED = 4401


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


class WebAuth:
    """Contraseña opcional + sesiones en memoria + freno a los intentos."""

    SESSION_TTL_SECONDS = 12 * 3600
    MAX_FAILURES = 5
    FAILURE_WINDOW_SECONDS = 300

    def __init__(self, password: str = ""):
        self.password = password or ""
        self._sessions: dict = {}  # token -> expira (time.time())
        self._failures: dict = defaultdict(deque)  # ip -> instantes de fallos

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def check_password(self, candidate: str) -> bool:
        # Comparación en tiempo constante: no revela por tiempos cuántos
        # caracteres del principio acertaste.
        return hmac.compare_digest((candidate or "").encode(), self.password.encode())

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + self.SESSION_TTL_SECONDS
        return token

    def is_valid(self, token: Optional[str]) -> bool:
        if not self.enabled:
            return True
        if not token:
            return False
        expira = self._sessions.get(token)
        if expira is None:
            return False
        if expira < time.time():
            del self._sessions[token]
            return False
        return True

    def revoke(self, token: Optional[str]) -> None:
        self._sessions.pop(token or "", None)

    def blocked(self, ip: str) -> bool:
        fallos = self._failures[ip]
        limite = time.time() - self.FAILURE_WINDOW_SECONDS
        while fallos and fallos[0] < limite:
            fallos.popleft()
        return len(fallos) >= self.MAX_FAILURES

    def record_failure(self, ip: str) -> None:
        self._failures[ip].append(time.time())


def create_app(manager: PositionManager, logs: LogBuffer, *, live: bool,
               allowed_hosts: Optional[list] = None, password: str = "") -> FastAPI:
    app = FastAPI(title="PePump", docs_url=None, redoc_url=None, openapi_url=None)
    allowed = allowed_hosts or ["127.0.0.1", "localhost"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)
    auth = WebAuth(password)

    def state() -> dict:
        return {"live": live, "server_time": time.time(), "positions": manager.snapshot()}

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def require_session(request: Request) -> None:
        if not auth.is_valid(request.cookies.get(SESSION_COOKIE)):
            raise HTTPException(status_code=401, detail="Hace falta entrar con la contraseña")

    def require_same_site(request: Request) -> None:
        """Toda orden tiene que venir de la propia página (ver docstring)."""
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status_code=403, detail="Petición rechazada (falta la cabecera del panel)")
        if not _origin_ok(request.headers.get("origin"), request.headers.get("host", ""), allowed):
            raise HTTPException(status_code=403, detail="Petición rechazada (otro origen)")

    lectura = [Depends(require_session)]
    orden = [Depends(require_same_site), Depends(require_session)]

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    # --- sesión ----------------------------------------------------------- #
    @app.get("/api/session")
    async def get_session(request: Request):
        return {"auth_required": auth.enabled,
                "authenticated": auth.is_valid(request.cookies.get(SESSION_COOKIE))}

    @app.post("/api/login", dependencies=[Depends(require_same_site)])
    async def login(request: Request, response: Response, password: str = Body(..., embed=True)):
        ip = client_ip(request)
        if not auth.enabled:
            return {"ok": True}
        if auth.blocked(ip):
            raise HTTPException(status_code=429, detail="Demasiados intentos fallidos. Espera unos minutos.")
        if not auth.check_password(password):
            auth.record_failure(ip)
            logger.warning(f"Panel web: contraseña incorrecta desde {ip}.")
            raise HTTPException(status_code=401, detail="Contraseña incorrecta")
        response.set_cookie(SESSION_COOKIE, auth.new_session(), max_age=auth.SESSION_TTL_SECONDS,
                            httponly=True, samesite="strict", path="/")
        logger.info(f"Panel web: sesión iniciada desde {ip}.")
        return {"ok": True}

    @app.post("/api/logout", dependencies=[Depends(require_same_site)])
    async def logout(request: Request, response: Response):
        auth.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    # --- lectura ---------------------------------------------------------- #
    @app.get("/api/state", dependencies=lectura)
    async def get_state():
        return state()

    @app.get("/api/positions/{pos_id}", dependencies=lectura)
    async def get_position(pos_id: int):
        managed = manager.get(pos_id)
        if managed is None:
            raise HTTPException(status_code=404, detail="No existe esa posición")
        return {"id": managed.id, **managed.bot.snapshot()}

    @app.get("/api/logs", dependencies=lectura)
    async def get_logs(mint: Optional[str] = None, after: int = 0, limit: int = 500):
        return logs.entries(mint=mint, after=after, limit=max(1, min(limit, 2000)))

    @app.get("/api/history", dependencies=lectura)
    async def get_history(limit: int = 100):
        return manager.history(limit=max(1, min(limit, 500)))

    # --- órdenes ---------------------------------------------------------- #
    @app.post("/api/positions/close-all", dependencies=orden)
    async def close_all(request: Request):
        # block_new=False: el servicio sigue vivo y más adelante se podrán
        # abrir posiciones nuevas desde la web.
        pedidas = manager.close_all(f"cerrar todas desde la web, {client_ip(request)}", block_new=False)
        return {"requested": pedidas}

    @app.post("/api/positions/{pos_id}/close", dependencies=orden)
    async def close_position(pos_id: int, request: Request):
        managed = manager.get(pos_id)
        if managed is None:
            raise HTTPException(status_code=404, detail="No existe esa posición")
        if not managed.closable:
            raise HTTPException(status_code=409, detail="Esa posición ya terminó o ya se está cerrando")
        manager.close(pos_id, f"cerrada desde la web, {client_ip(request)}")
        return {"id": managed.id, **managed.bot.snapshot()}

    # --- en vivo ---------------------------------------------------------- #
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if not _origin_ok(websocket.headers.get("origin"), websocket.headers.get("host", ""), allowed):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        if not auth.is_valid(websocket.cookies.get(SESSION_COOKIE)):
            await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
            return
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
                    # La sesión puede caducar (o cerrarse) con el WebSocket abierto.
                    if not auth.is_valid(websocket.cookies.get(SESSION_COOKIE)):
                        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
                        return
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


def _origin_ok(origin: Optional[str], host_header: str, allowed: list) -> bool:
    if origin is None:
        # Sin Origin no es un navegador haciendo una petición entre webs
        # (curl, ctl.py, o un GET normal): no hay riesgo de página ajena.
        return True
    partes = urlsplit(origin)
    return partes.netloc == host_header and ("*" in allowed or partes.hostname in allowed)


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
