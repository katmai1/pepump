"""
Tests del feed compartido (feed.py): un websocket real para todas las
posiciones, con una suscripción virtual por mint.
"""
import asyncio
import json

import pytest

from pepump.feed import SharedTradeFeed

A = "MintA111111111111111111111111111111111111111"
B = "MintB111111111111111111111111111111111111111"


class FakeWS:
    """Websocket falso: lo que el test mete con push() sale por la
    iteración; drop() simula que PumpPortal corta la conexión."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self._q = asyncio.Queue()

    async def send(self, msg):
        if self.closed:
            raise ConnectionError("cerrado")
        self.sent.append(json.loads(msg))

    async def close(self):
        self.closed = True
        self._q.put_nowait(None)

    def push(self, obj):
        self._q.put_nowait(json.dumps(obj))

    def drop(self):
        self._q.put_nowait(ConnectionError("se cayó (fake)"))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    DATA_WS_URL = "wss://fake/api/data"
    api_key = "k"

    @staticmethod
    def extract_price(event):
        return event.get("price")


class Conexiones:
    def __init__(self, fail_first: int = 0):
        self.sockets = []
        self.fail_first = fail_first

    async def __call__(self, url):
        if self.fail_first:
            self.fail_first -= 1
            raise OSError("sin red (fake)")
        ws = FakeWS()
        self.sockets.append(ws)
        return ws


async def _siguiente(it, timeout=1.0):
    return await asyncio.wait_for(it.__anext__(), timeout)


def _feed(**kw):
    conns = Conexiones(**kw)
    feed = SharedTradeFeed(FakeClient(), connect=conns)
    feed._INITIAL_BACKOFF = feed._MAX_BACKOFF = 0.01
    return feed, conns


async def test_un_solo_websocket_para_varios_mints():
    feed, conns = _feed()
    await feed.connect_trade_stream(A)
    await feed.connect_trade_stream(B)

    assert len(conns.sockets) == 1
    assert conns.sockets[0].sent == [
        {"method": "subscribeTokenTrade", "keys": [A]},
        {"method": "subscribeTokenTrade", "keys": [B]},
    ]
    await feed.close()


async def test_cada_suscripcion_recibe_solo_su_mint_y_su_ack():
    feed, conns = _feed()
    sa = await feed.connect_trade_stream(A)
    sb = await feed.connect_trade_stream(B)
    ita, itb = feed.iter_trade_events(sa), feed.iter_trade_events(sb)
    ws = conns.sockets[0]

    ws.push({"message": "Successfully subscribed to keys."})  # ack de A
    ws.push({"message": "Successfully subscribed to keys."})  # ack de B
    ws.push({"mint": B, "price": 2.0})
    ws.push({"mint": A, "price": 1.0})
    ws.push({"mint": "OtroMint", "price": 9.0})  # nadie lo pidió

    assert await _siguiente(ita) == {"message": "Successfully subscribed to keys."}
    assert await _siguiente(ita) == {"mint": A, "price": 1.0}
    assert await _siguiente(itb) == {"message": "Successfully subscribed to keys."}
    assert await _siguiente(itb) == {"mint": B, "price": 2.0}
    assert sa.queue.empty() and sb.queue.empty()
    await feed.close()


async def test_close_da_de_baja_y_cierra_la_conexion_con_el_ultimo():
    feed, conns = _feed()
    sa = await feed.connect_trade_stream(A)
    sb = await feed.connect_trade_stream(B)
    ws = conns.sockets[0]

    await sa.close()
    assert {"method": "unsubscribeTokenTrade", "keys": [A]} in ws.sent
    assert not ws.closed
    # El iterador de la suscripción cerrada termina (como un ws cerrado).
    with pytest.raises(StopAsyncIteration):
        await _siguiente(feed.iter_trade_events(sa))

    await sb.close()
    assert ws.closed
    assert feed.active_mints == set()

    # Una suscripción nueva abre otra conexión.
    await feed.connect_trade_stream(A)
    assert len(conns.sockets) == 2
    await feed.close()


async def test_reconecta_y_resuscribe_sin_que_el_bot_lo_note():
    feed, conns = _feed()
    sa = await feed.connect_trade_stream(A)
    await feed.connect_trade_stream(B)
    ita = feed.iter_trade_events(sa)
    conns.sockets[0].push({"message": "ok"})
    conns.sockets[0].push({"message": "ok"})
    assert await _siguiente(ita) == {"message": "ok"}

    conns.sockets[0].drop()
    while len(conns.sockets) < 2 or len(conns.sockets[1].sent) < 2:
        await asyncio.sleep(0.01)
    nuevo = conns.sockets[1]
    assert sorted(m["keys"][0] for m in nuevo.sent) == sorted([A, B])

    # Los acks de la re-suscripción no le llegan a quien ya tenía el suyo.
    nuevo.push({"message": "ok"})
    nuevo.push({"message": "ok"})
    nuevo.push({"mint": A, "price": 3.0})
    assert await _siguiente(ita) == {"mint": A, "price": 3.0}
    await feed.close()


async def test_tras_reconectar_el_ack_va_a_quien_todavia_no_lo_tenia():
    feed, conns = _feed()
    sa = await feed.connect_trade_stream(A)
    ita = feed.iter_trade_events(sa)
    conns.sockets[0].drop()  # se cae antes de que llegue el ack de A
    while len(conns.sockets) < 2 or not conns.sockets[1].sent:
        await asyncio.sleep(0.01)
    conns.sockets[1].push({"message": "ok"})
    assert await _siguiente(ita) == {"message": "ok"}
    await feed.close()


async def test_si_falla_la_primera_conexion_el_error_sube_al_bot():
    feed, conns = _feed(fail_first=1)
    with pytest.raises(OSError):
        await feed.connect_trade_stream(A)
    assert feed.active_mints == set()
    # Y el siguiente intento conecta normalmente.
    await feed.connect_trade_stream(A)
    assert len(conns.sockets) == 1
    await feed.close()


async def test_el_ack_de_un_unsubscribe_no_se_reparte():
    feed, conns = _feed()
    sa = await feed.connect_trade_stream(A)
    sb = await feed.connect_trade_stream(B)
    ws = conns.sockets[0]
    ws.push({"message": "ok"})  # ack de A
    await sa.close()
    ws.push({"message": "Successfully unsubscribed from keys."})
    ws.push({"message": "ok"})  # ack de B
    itb = feed.iter_trade_events(sb)
    assert await _siguiente(itb) == {"message": "ok"}
    await feed.close()
