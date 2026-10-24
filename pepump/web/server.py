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
  GET  /api/positions/{id}/prices     precios para el gráfico (?after=<ts>)
  GET  /api/wallet                    dirección y saldo de SOL de la wallet
  GET  /api/defaults                  ajustes por defecto y límites (formulario)
  GET  /api/preview?mint=...&buy_sol= qué es un mint antes de comprar
  POST /api/positions                 abrir (comprar) una posición
  POST /api/positions/{id}/close      cerrar (vender) una posición
  POST /api/positions/close-all       cerrar todas
  GET  /api/logs                      últimos logs (?mint=...&after=<seq>&limit=...)
  GET  /api/history                   posiciones terminadas + resumen de PnL
  POST /api/history/clear             borrar el historial ({"mode": "all"|"real"|"sim"})
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
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pepump.manager import OVERRIDABLE_FIELDS, PositionManager
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
# La vista previa de un mint se recuerda unos segundos: el formulario la
# pide mientras escribes/pegas y cada una son consultas al RPC.
PREVIEW_CACHE_SECONDS = 10.0
# Cada cuánto mira cada WebSocket si el servidor se está apagando.
_SHUTDOWN_CHECK_SECONDS = 0.2


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
    # Al apagar (WebPanel.stop) se marca esto y cada WebSocket se cierra
    # solo, de forma ordenada, antes de que uvicorn pare.
    app.state.shutting_down = asyncio.Event()
    app.state.ws_open = 0
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
        # BUGFIX (botones que "no hacían nada" tras actualizar el bot): el
        # HTML se servía siempre fresco, pero app.js/style.css no, y el
        # navegador podía seguir usando una versión vieja de su caché que no
        # casaba con el HTML nuevo. Ahora cada referencia lleva la versión
        # del archivo (?v=...), así que al cambiar se piden de nuevo seguro.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for asset in ("app.js", "chart.js", "style.css"):
            html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={_asset_version(asset)}\"")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Que el navegador pregunte siempre si cambió (con ETag, sin
            # volver a bajarlo si no): nunca un JS viejo.
            response.headers["Cache-Control"] = "no-cache"
        return response

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

    @app.get("/api/positions/{pos_id}/prices", dependencies=lectura)
    async def get_prices(pos_id: int, after: float = 0.0, max_points: int = 1500):
        managed = manager.get(pos_id)
        if managed is None:
            raise HTTPException(status_code=404, detail="No existe esa posición")
        points = [p for p in managed.bot.price_history if p[0] > after]
        return {"points": downsample(points, max(50, min(max_points, 5000)))}

    @app.get("/api/wallet", dependencies=lectura)
    async def get_wallet(fresh: bool = False):
        # fresh=1 (al pulsar sobre el saldo): sin la caché de 20 s.
        return await manager.wallet_info(max_age=0 if fresh else 20.0)

    @app.get("/api/logs", dependencies=lectura)
    async def get_logs(mint: Optional[str] = None, after: int = 0, limit: int = 500):
        return logs.entries(mint=mint, after=after, limit=max(1, min(limit, 2000)))

    @app.get("/api/history", dependencies=lectura)
    async def get_history(limit: int = 100):
        return {"rows": manager.history(limit=max(1, min(limit, 500))),
                "summary": manager.history_summary()}

    @app.get("/api/defaults", dependencies=lectura)
    async def get_defaults():
        return manager.defaults()

    preview_cache: dict = {}  # mint -> (instante, resultado)

    # Con la cabecera del panel aunque sea un GET: cada vista previa gasta
    # consultas del RPC, y así una web ajena no puede ponerse a lanzarlas.
    @app.get("/api/preview", dependencies=orden)
    async def get_preview(mint: str, buy_sol: Optional[float] = None):
        mint = mint.strip()
        # El coste de entrar depende del importe (impacto): va en la clave.
        key = (mint, buy_sol)
        cached = preview_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < PREVIEW_CACHE_SECONDS:
            result = cached[1]
        else:
            try:
                result = await manager.preview(mint, buy_sol)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            preview_cache[key] = (time.monotonic(), result)
            if len(preview_cache) > 200:
                preview_cache.clear()
        # La posición activa sí se mira siempre al momento.
        managed = manager.get(result["mint"])
        return {**result, "active_position": managed.id if managed is not None and managed.active else None}

    # --- órdenes ---------------------------------------------------------- #
    @app.post("/api/history/clear", dependencies=orden)
    async def clear_history(body: dict = Body(default={})):
        modes = {"all": None, "real": True, "sim": False}
        mode = body.get("mode", "all")
        if mode not in modes:
            raise HTTPException(status_code=400, detail="mode debe ser all, real o sim")
        return {"deleted": manager.clear_history(live=modes[mode])}

    @app.post("/api/positions", dependencies=orden)
    async def open_position(request: Request, body: dict = Body(...)):
        mint = body.get("mint")
        settings = body.get("settings") or {}
        if not isinstance(mint, str) or not isinstance(settings, dict):
            raise HTTPException(status_code=400, detail="Falta el mint")
        overrides = {}
        for key, value in settings.items():
            if key not in OVERRIDABLE_FIELDS:
                raise HTTPException(status_code=400, detail=f"Ajuste desconocido: {key}")
            if key == "live":
                if not isinstance(value, bool):
                    raise HTTPException(status_code=400, detail="live tiene que ser true o false")
            elif key in ("pool", "strategy_price_basis"):
                if not isinstance(value, str):
                    raise HTTPException(status_code=400, detail=f"{key} tiene que ser texto")
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(status_code=400, detail=f"{key} tiene que ser un número")
            else:
                value = float(value)
            overrides[key] = value
        defaults = manager.defaults()["settings"]
        live = overrides.get("live", defaults["live"])
        try:
            if live:
                # Saldo fresco antes de una compra REAL (ver check_funds).
                await manager.check_funds(overrides.get("buy_sol", defaults["buy_sol"]),
                                          overrides.get("priority_fee", defaults["priority_fee"]))
            managed = manager.open(mint, **overrides)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        modo = "REAL" if managed.config.live else "simulada"
        logger.info(f"Posición #{managed.id} ({modo}) abierta desde la web ({client_ip(request)}).")
        return {"id": managed.id, **managed.bot.snapshot()}

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
        app.state.ws_open += 1
        try:
            await websocket.send_text(json.dumps({
                "type": "hello", "state": state(), "logs": logs.entries(limit=INITIAL_LOGS),
            }))
            next_state = time.monotonic() + STATE_INTERVAL_SECONDS
            while True:
                if app.state.shutting_down.is_set():
                    # 1001 = "going away": el navegador reintenta solo.
                    await websocket.close(code=1001)
                    return
                timeout = min(max(0.0, next_state - time.monotonic()), _SHUTDOWN_CHECK_SECONDS)
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    if time.monotonic() < next_state:
                        continue  # solo tocaba mirar si se está apagando
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
        except asyncio.CancelledError:
            # uvicorn cancela a la fuerza lo que siga abierto al apagar (si
            # el cierre ordenado no llegó a tiempo). No es un error: sin
            # esto, uvicorn lo registra como "Exception in ASGI application".
            pass
        finally:
            app.state.ws_open -= 1
            logs.unsubscribe(queue)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def downsample(points: list, max_points: int) -> list:
    """Reduce una serie (ts, precio) a ~max_points para el gráfico SIN
    perder picos: por cada tramo se quedan su mínimo y su máximo (en orden
    de tiempo), además del primer y el último punto de la serie."""
    if len(points) <= max_points:
        return [list(p) for p in points]
    buckets = max(1, max_points // 2)
    size = len(points) / buckets
    out = [list(points[0])]
    for i in range(buckets):
        chunk = points[int(i * size):int((i + 1) * size)]
        if not chunk:
            continue
        lo = min(chunk, key=lambda p: p[1])
        hi = max(chunk, key=lambda p: p[1])
        for p in sorted({lo, hi}, key=lambda p: p[0]):
            out.append(list(p))
    out.append(list(points[-1]))
    return out


def _asset_version(name: str) -> str:
    stat = (STATIC_DIR / name).stat()
    return f"{int(stat.st_mtime)}-{stat.st_size}"


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
        self._app = app
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
        # Primero, que cada WebSocket abierto se cierre de forma ordenada
        # (ver el bucle de /ws); hasta 2 s, por si alguno está enviando.
        self._app.state.shutting_down.set()
        deadline = time.monotonic() + 2.0
        while self._app.state.ws_open > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self._server.should_exit = True
        # Las conexiones WebSocket abiertas no terminan solas: sin esto,
        # uvicorn esperaría a que el navegador las cierre.
        self._server.force_exit = True
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(self._task, timeout=5)
        self._task = None
