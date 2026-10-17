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


class FakeManager:
    def __init__(self, snaps):
        self.snaps = snaps

    def snapshot(self):
        return self.snaps


SNAP = {"id": 1, "mint": A, "phase": "open", "running": True, "position": None}


def make_client(snaps=(SNAP,), logs=None, live=False):
    logs = logs or LogBuffer()
    app = create_app(FakeManager(list(snaps)), logs, live=live, allowed_hosts=allowed_hosts_for("127.0.0.1"))
    # base_url con un Host permitido (el de por defecto, "testserver", no lo es).
    return TestClient(app, base_url="http://127.0.0.1:8080"), logs


def test_state_y_posiciones():
    client, _ = make_client(live=True)
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is True
    assert body["positions"] == [SNAP]

    assert client.get("/api/positions/1").json() == SNAP
    assert client.get("/api/positions/2").status_code == 404


def test_la_pagina_y_los_estaticos():
    client, _ = make_client()
    r = client.get("/")
    assert r.status_code == 200 and "<title>PePump</title>" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_no_hay_nada_que_compre_o_venda():
    client, _ = make_client()
    for path in ("/api/state", "/api/positions/1", "/api/logs"):
        assert client.post(path).status_code == 405
        assert client.delete(path).status_code == 405


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
