"""
Tests del panel web de solo lectura (pepump/web).
"""
import asyncio
import logging
import threading

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pepump.log_context import current_mint
from pepump.web.logbuffer import LogBuffer
from pepump.web.server import allowed_hosts_for, create_app, is_loopback

A = "So11111111111111111111111111111111111111112"


class FakeBot:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self):
        return {k: v for k, v in self.snap.items() if k != "id"}


class FakeManaged:
    def __init__(self, snap):
        self.id = snap["id"]
        self.bot = FakeBot(snap)

    @property
    def closable(self):
        return self.bot.snap["running"] and not self.bot.snap.get("close_requested")


class FakeManager:
    def __init__(self, snaps):
        self.snaps = snaps
        self.closed = []      # (id, reason)
        self.close_all_calls = []  # (reason, block_new)

    def snapshot(self):
        return self.snaps

    def get(self, ref):
        for snap in self.snaps:
            if snap["id"] == ref:
                return FakeManaged(snap)
        return None

    def close(self, ref, reason="cierre manual"):
        self.closed.append((ref, reason))
        for snap in self.snaps:
            if snap["id"] == ref:
                snap["close_requested"] = True

    def history(self, limit=100):
        return [{"id": 99, "mint": A, "status": "closed", "realized_pnl_pct": 12.5}][:limit]

    def close_all(self, reason="", block_new=True):
        self.close_all_calls.append((reason, block_new))
        return len(self.snaps)


SNAP = {"id": 1, "mint": A, "phase": "open", "running": True, "close_requested": False, "position": None}
ORIGIN = "http://127.0.0.1:8080"
# Cabeceras que manda la propia página en cada orden.
PAGE = {"x-pepump": "1", "origin": ORIGIN}


def make_client(snaps=(SNAP,), logs=None, live=False, password=""):
    logs = logs or LogBuffer()
    manager = FakeManager([dict(s) for s in snaps])
    app = create_app(manager, logs, live=live, allowed_hosts=allowed_hosts_for("127.0.0.1"),
                     password=password)
    # base_url con un Host permitido (el de por defecto, "testserver", no lo es).
    client = TestClient(app, base_url=ORIGIN)
    client.manager = manager
    return client, logs


def test_state_y_posiciones():
    client, _ = make_client(live=True)
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is True
    assert body["positions"] == [SNAP]

    assert client.get("/api/positions/1").json() == SNAP
    assert client.get("/api/positions/2").status_code == 404


def test_historial():
    client, _ = make_client()
    assert client.get("/api/history").json()[0]["id"] == 99
    assert client.post("/api/history", headers=PAGE).status_code == 405


def test_la_pagina_y_los_estaticos():
    client, _ = make_client()
    r = client.get("/")
    assert r.status_code == 200 and "<title>PePump</title>" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_las_rutas_de_lectura_no_aceptan_ordenes():
    client, _ = make_client()
    for path in ("/api/state", "/api/positions/1", "/api/logs"):
        assert client.post(path, headers=PAGE).status_code == 405
        assert client.delete(path, headers=PAGE).status_code == 405


def test_no_hay_ninguna_ruta_que_compre():
    client, _ = make_client()
    rutas = [r.path for r in client.app.routes]
    assert not any("buy" in r or "open" in r for r in rutas)


# --------------------------------------------------------------------------- #
# Cerrar posiciones
# --------------------------------------------------------------------------- #

def test_cerrar_una_posicion():
    client, _ = make_client()
    r = client.post("/api/positions/1/close", headers=PAGE)
    assert r.status_code == 200
    ((pos_id, reason),) = client.manager.closed
    assert pos_id == 1
    assert "desde la web" in reason
    # Ya pedido: un segundo intento no vuelve a mandar nada.
    assert client.post("/api/positions/1/close", headers=PAGE).status_code == 409
    assert len(client.manager.closed) == 1


def test_cerrar_una_posicion_que_no_existe_o_ya_termino():
    terminada = dict(SNAP, id=2, running=False, phase="closed")
    client, _ = make_client(snaps=(SNAP, terminada))
    assert client.post("/api/positions/9/close", headers=PAGE).status_code == 404
    assert client.post("/api/positions/2/close", headers=PAGE).status_code == 409
    assert client.manager.closed == []


def test_cerrar_todas_no_bloquea_abrir_despues():
    client, _ = make_client()
    r = client.post("/api/positions/close-all", headers=PAGE)
    assert r.status_code == 200 and r.json() == {"requested": 1}
    ((reason, block_new),) = client.manager.close_all_calls
    assert "desde la web" in reason
    assert block_new is False


def test_una_web_ajena_no_puede_cerrar():
    """Sin contraseña, lo único que impide que otra página abierta en tu
    navegador te cierre posiciones es esto (CSRF)."""
    client, _ = make_client()
    # Sin la cabecera del panel (un <form> de otra web no puede ponerla).
    assert client.post("/api/positions/1/close").status_code == 403
    assert client.post("/api/positions/close-all", headers={"origin": ORIGIN}).status_code == 403
    # Con la cabecera pero desde otro origen.
    ajena = {"x-pepump": "1", "origin": "https://evil.example"}
    assert client.post("/api/positions/1/close", headers=ajena).status_code == 403
    assert client.manager.closed == [] and client.manager.close_all_calls == []


# --------------------------------------------------------------------------- #
# Contraseña (opcional)
# --------------------------------------------------------------------------- #

def test_sin_contrasena_no_pide_nada():
    client, _ = make_client()
    assert client.get("/api/session").json() == {"auth_required": False, "authenticated": True}
    assert client.get("/api/state").status_code == 200


def test_con_contrasena_hay_que_entrar():
    client, _ = make_client(password="una-clave-larga")
    assert client.get("/api/session").json() == {"auth_required": True, "authenticated": False}
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/logs").status_code == 401
    assert client.post("/api/positions/1/close", headers=PAGE).status_code == 401

    assert client.post("/api/login", json={"password": "mal"}, headers=PAGE).status_code == 401
    r = client.post("/api/login", json={"password": "una-clave-larga"}, headers=PAGE)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie

    assert client.get("/api/session").json()["authenticated"] is True
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/positions/1/close", headers=PAGE).status_code == 200

    client.post("/api/logout", headers=PAGE)
    assert client.get("/api/state").status_code == 401


def test_frena_los_intentos_de_adivinar_la_contrasena():
    client, _ = make_client(password="una-clave-larga")
    for _ in range(5):
        assert client.post("/api/login", json={"password": "x"}, headers=PAGE).status_code == 401
    # Bloqueado aunque ahora acierte.
    r = client.post("/api/login", json={"password": "una-clave-larga"}, headers=PAGE)
    assert r.status_code == 429


def test_websocket_sin_sesion_se_cierra_con_4401():
    client, _ = make_client(password="una-clave-larga")
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("ws://127.0.0.1:8080/ws", headers={"origin": ORIGIN}) as ws:
            ws.receive_json()
    assert e.value.code == 4401

    client.post("/api/login", json={"password": "una-clave-larga"}, headers=PAGE)
    with client.websocket_connect("ws://127.0.0.1:8080/ws", headers={"origin": ORIGIN}) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_rechaza_un_host_ajeno():
    """DNS rebinding: un dominio ajeno apuntando a 127.0.0.1."""
    client, _ = make_client()
    r = client.get("/api/state", headers={"host": "evil.example:8080"})
    assert r.status_code == 400


def test_logs_filtrados_por_mint_y_seq():
    logs = LogBuffer()
    log = logging.getLogger("pepump.test_web")
    log.addHandler(logs)
    log.setLevel(logging.INFO)
    try:
        log.info("general")
        tok = current_mint.set(A)
        try:
            log.info("de la posición")
        finally:
            current_mint.reset(tok)
    finally:
        log.removeHandler(logs)

    client, _ = make_client(logs=logs)
    todos = client.get("/api/logs").json()
    assert [e["message"] for e in todos] == ["general", "de la posición"]
    assert [e["mint"] for e in todos] == ["", A]
    assert [e["message"] for e in client.get("/api/logs", params={"mint": A}).json()] == ["de la posición"]
    assert client.get("/api/logs", params={"after": todos[0]["seq"]}).json() == [todos[1]]


def test_websocket_manda_hello_estado_y_logs():
    client, logs = make_client()
    with client.websocket_connect("ws://127.0.0.1:8080/ws", headers={"origin": "http://127.0.0.1:8080"}) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["state"]["positions"] == [SNAP]
        # Un log nuevo llega en el momento (desde otro hilo, como to_thread).
        record = logging.LogRecord("pepump.x", logging.INFO, __file__, 1, "hola web", None, None)
        t = threading.Thread(target=logs.handle, args=(record,))
        t.start(); t.join()
        tipos = set()
        for _ in range(5):
            msg = ws.receive_json()
            tipos.add(msg["type"])
            if msg["type"] == "logs":
                assert msg["logs"][-1]["message"] == "hola web"
                break
        assert "logs" in tipos


def test_websocket_rechaza_otro_origin():
    """Los WebSocket no tienen CORS: sin esta comprobación cualquier web
    abierta en el navegador podría leer el estado del bot."""
    client, _ = make_client()
    # Se cierra antes de aceptar: un navegador recibe el handshake rechazado.
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("ws://127.0.0.1:8080/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 1008


def test_hosts_permitidos_y_loopback():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.5")
    assert allowed_hosts_for("0.0.0.0") == ["*"]
    assert "192.168.1.5" in allowed_hosts_for("192.168.1.5")


async def test_logbuffer_descarta_si_un_cliente_no_lee():
    logs = LogBuffer()
    logs._LISTENER_QUEUE_MAX = 3
    q = logs.subscribe()
    for i in range(10):
        logs.handle(logging.LogRecord("x", logging.INFO, __file__, 1, f"m{i}", None, None))
    await asyncio.sleep(0)
    assert q.qsize() <= 3
    assert len(logs.entries()) == 10
    logs.unsubscribe(q)
