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
        self.bot.price_history = [(100.0 + i, 1.0 + i) for i in range(10)]
        self.config = type("Cfg", (), {"live": snap.get("live", False)})()

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

    def defaults(self):
        return {"live": False, "settings": {"live": False, "buy_sol": 0.05, "priority_fee": 1e-5},
                "max_buy_sol": 0.5,
                "max_open_positions": 10, "active_positions": 1, "accepting_new": True}

    wallet_sol = 1.0

    async def wallet_info(self, max_age=20):
        return {"address": "W", "sol": self.wallet_sol, "source": "config"}

    async def check_funds(self, buy_sol, priority_fee):
        self.funds_checked = (buy_sol, priority_fee)
        if buy_sol + priority_fee > self.wallet_sol:
            raise ValueError("Saldo insuficiente: la wallet tiene 0.0100 SOL")

    async def preview(self, mint):
        if mint == "malo":
            raise ValueError("'malo' no es una dirección de mint válida")
        self.previews = getattr(self, "previews", 0) + 1
        return {"mint": mint, "name": "Pepe", "symbol": "PEPE", "market": "bondingcurve",
                "price": 1e-7, "active_position": None}

    def open(self, mint, **overrides):
        if overrides.get("buy_sol", 0) > 0.5:
            raise ValueError("trade.buy_sol (10.0 SOL) supera el máximo por posición")
        self.opened = (mint, overrides)
        snap = dict(SNAP, id=5, mint=mint)
        self.snaps.append(snap)
        return FakeManaged(snap)

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


def test_los_estaticos_llevan_version_y_no_se_cachean_a_ciegas():
    """Un app.js viejo en la caché del navegador con el HTML nuevo dejaba
    botones sin hacer nada."""
    import re
    client, _ = make_client()
    html = client.get("/").text
    assert re.search(r'/static/app\.js\?v=\d+-\d+"', html)
    assert re.search(r'/static/style\.css\?v=\d+-\d+"', html)
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"


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
        t.start()
        t.join()
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


# --------------------------------------------------------------------------- #
# Abrir posiciones (paso 6)
# --------------------------------------------------------------------------- #

def test_defaults():
    client, _ = make_client()
    assert client.get("/api/defaults").json()["max_buy_sol"] == 0.5


def test_abrir_una_posicion():
    client, _ = make_client()
    r = client.post("/api/positions", headers=PAGE,
                    json={"mint": A, "settings": {"buy_sol": 0.1, "trailing_pct": 12, "pool": "auto"}})
    assert r.status_code == 200 and r.json()["id"] == 5
    mint, overrides = client.manager.opened
    assert mint == A
    assert overrides == {"buy_sol": 0.1, "trailing_pct": 12.0, "pool": "auto"}


def test_abrir_eligiendo_real_o_simulada():
    client, _ = make_client()
    r = client.post("/api/positions", headers=PAGE, json={"mint": A, "settings": {"live": True}})
    assert r.status_code == 200
    assert client.manager.opened[1] == {"live": True}
    # Tiene que ser un booleano de verdad: ni "true" ni 1.
    for malo in ("true", 1):
        r = client.post("/api/positions", headers=PAGE, json={"mint": A, "settings": {"live": malo}})
        assert r.status_code == 400


def test_abrir_valida_lo_que_llega():
    client, _ = make_client()
    malos = [
        {"settings": {}},                                       # sin mint
        {"mint": A, "settings": {"api_key": "x"}},              # no ajustable por posición
        {"mint": A, "settings": {"buy_sol": "mucho"}},          # no es un número
        {"mint": A, "settings": {"buy_sol": True}},             # bool no cuenta como número
        {"mint": A, "settings": {"pool": 3}},                   # pool tiene que ser texto
    ]
    for body in malos:
        assert client.post("/api/positions", headers=PAGE, json=body).status_code == 400, body
    # El límite lo aplica el gestor y el error llega legible.
    r = client.post("/api/positions", headers=PAGE, json={"mint": A, "settings": {"buy_sol": 10}})
    assert r.status_code == 400 and "máximo" in r.json()["detail"]
    assert not hasattr(client.manager, "opened")


def test_una_web_ajena_no_puede_abrir():
    client, _ = make_client()
    assert client.post("/api/positions", json={"mint": A}).status_code == 403
    ajena = {"x-pepump": "1", "origin": "https://evil.example"}
    assert client.post("/api/positions", headers=ajena, json={"mint": A}).status_code == 403
    assert not hasattr(client.manager, "opened")


def test_preview_con_cache_y_errores():
    client, _ = make_client()
    r = client.get("/api/preview", params={"mint": A}, headers=PAGE)
    assert r.status_code == 200 and r.json()["symbol"] == "PEPE"
    client.get("/api/preview", params={"mint": A}, headers=PAGE)
    assert client.manager.previews == 1  # la segunda sale de la caché
    assert client.get("/api/preview", params={"mint": "malo"}, headers=PAGE).status_code == 400
    # Sin la cabecera del panel no se gasta RPC.
    assert client.get("/api/preview", params={"mint": A}).status_code == 403



# --------------------------------------------------------------------------- #
# Wallet y gráfico (paso 7)
# --------------------------------------------------------------------------- #

def test_wallet():
    client, _ = make_client()
    assert client.get("/api/wallet").json() == {"address": "W", "sol": 1.0, "source": "config"}


def test_compra_real_comprueba_el_saldo_antes():
    client, _ = make_client()
    client.manager.wallet_sol = 0.01
    r = client.post("/api/positions", headers=PAGE, json={"mint": A, "settings": {"live": True}})
    assert r.status_code == 400 and "Saldo insuficiente" in r.json()["detail"]
    assert client.manager.funds_checked == (0.05, 1e-5)
    assert not hasattr(client.manager, "opened")
    # Una simulada no mira el saldo.
    client.manager.funds_checked = None
    r = client.post("/api/positions", headers=PAGE, json={"mint": A, "settings": {"live": False}})
    assert r.status_code == 200 and client.manager.funds_checked is None


def test_precios_para_el_grafico():
    client, _ = make_client()
    puntos = client.get("/api/positions/1/prices").json()["points"]
    assert len(puntos) == 10 and puntos[0] == [100.0, 1.0]
    # Solo los nuevos (para ir actualizando el gráfico).
    nuevos = client.get("/api/positions/1/prices", params={"after": 107.0}).json()["points"]
    assert nuevos == [[108.0, 9.0], [109.0, 10.0]]
    assert client.get("/api/positions/9/prices").status_code == 404


def test_downsample_conserva_los_picos():
    from pepump.web.server import downsample
    serie = [(float(i), 1.0) for i in range(10_000)]
    serie[5_000] = (5_000.0, 50.0)   # un pico de un solo punto
    serie[7_000] = (7_000.0, 0.01)   # una caída de un solo punto
    out = downsample(serie, 200)
    assert len(out) <= 205
    precios = [p[1] for p in out]
    assert 50.0 in precios and 0.01 in precios
    assert out[0] == [0.0, 1.0] and out[-1] == [9_999.0, 1.0]
    assert [p[0] for p in out] == sorted(p[0] for p in out)
