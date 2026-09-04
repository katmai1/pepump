"""
Tests para PumpCurveOnChainClient: el fallback que lee el precio DIRECTO
de la cuenta de la bonding curve de pump.fun por RPC, para cuando
subscribeTokenTrade da el ack pero no entrega ningún trade real para un
mint que sigue en bonding curve (ver bot.py:_try_onchain_fallback /
_handle_feed_stall).

No pega a la red real: monkeypatchea pump_module.AsyncClient con un
fake que devuelve bytes armados a mano con el mismo layout que la
cuenta real (ver PUMP_CURVE_STATE_FIELD_OFFSETS documentado en la
propia clase).
"""
import asyncio
import struct

from pepump import pump as pump_module
from pepump.pump import PumpCurveOnChainClient

FAKE_MINT = "So11111111111111111111111111111111111111112"  # cualquier mint válido, no se valida contra la red


def _make_account_data(virtual_token_reserves: int, virtual_sol_reserves: int,
                        complete: bool, extra_bytes: int = 0) -> bytes:
    """Arma bytes crudos con el mismo layout que la cuenta real:
    8 (discriminador, valor cualquiera acá) + u64 virtualTokenReserves +
    u64 virtualSolReserves + u64 realTokenReserves (0, no se usa) +
    u64 realSolReserves (0, no se usa) + u64 tokenTotalSupply (0, no se
    usa) + 1 byte complete + `extra_bytes` de relleno (simula los campos
    nuevos que fue sumando el programa -cashback, etc.- sin romper los
    offsets viejos)."""
    return (
        b"\x00" * 8
        + struct.pack("<Q", virtual_token_reserves)
        + struct.pack("<Q", virtual_sol_reserves)
        + struct.pack("<Q", 0)  # realTokenReserves
        + struct.pack("<Q", 0)  # realSolReserves
        + struct.pack("<Q", 0)  # tokenTotalSupply
        + struct.pack("<?", complete)
        + b"\x00" * extra_bytes
    )


class FakeAccountInfoValue:
    def __init__(self, data: bytes):
        self.data = data


class FakeAccountInfoResp:
    def __init__(self, value):
        self.value = value


class FakeAsyncClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_account_info(self, pubkey, encoding="base64"):
        self.calls += 1
        return self._resp


def test_precio_normal_curva_activa(monkeypatch):
    # 30 SOL / 1.073B tokens (reservas virtuales iniciales típicas de
    # pump.fun) -> precio esperado ~2.796e-8 SOL/token.
    data = _make_account_data(
        virtual_token_reserves=1_073_000_000 * 10**6,
        virtual_sol_reserves=30 * 10**9,
        complete=False,
    )
    fake_client = FakeAsyncClient(FakeAccountInfoResp(FakeAccountInfoValue(data)))
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    client = PumpCurveOnChainClient("http://fake-rpc")
    price, complete, exists = asyncio.run(client.fetch_price_or_status(FAKE_MINT))

    assert exists is True
    assert complete is False
    assert abs(price - (30 / 1_073_000_000)) < 1e-12


def test_layout_tolera_bytes_extra_al_final(monkeypatch):
    """La cuenta real CRECIÓ con el tiempo (cashback, mayhem mode, etc.)
    pero los offsets viejos no cambiaron -el parseo no debe romperse
    por bytes extra al final."""
    data = _make_account_data(
        virtual_token_reserves=1_073_000_000 * 10**6,
        virtual_sol_reserves=30 * 10**9,
        complete=False,
        extra_bytes=200,
    )
    fake_client = FakeAsyncClient(FakeAccountInfoResp(FakeAccountInfoValue(data)))
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    client = PumpCurveOnChainClient("http://fake-rpc")
    price, complete, exists = asyncio.run(client.fetch_price_or_status(FAKE_MINT))

    assert exists is True
    assert abs(price - (30 / 1_073_000_000)) < 1e-12


def test_curva_completada_devuelve_complete_true(monkeypatch):
    data = _make_account_data(
        virtual_token_reserves=1,  # ya casi sin tokens virtuales -> curva llena
        virtual_sol_reserves=115 * 10**9,
        complete=True,
    )
    fake_client = FakeAsyncClient(FakeAccountInfoResp(FakeAccountInfoValue(data)))
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    client = PumpCurveOnChainClient("http://fake-rpc")
    price, complete, exists = asyncio.run(client.fetch_price_or_status(FAKE_MINT))

    assert exists is True
    assert complete is True


def test_cuenta_inexistente(monkeypatch):
    fake_client = FakeAsyncClient(FakeAccountInfoResp(None))
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    client = PumpCurveOnChainClient("http://fake-rpc")
    price, complete, exists = asyncio.run(client.fetch_price_or_status(FAKE_MINT))

    assert price is None
    assert complete is False
    assert exists is False


def test_virtual_token_reserves_cero_no_calcula_precio(monkeypatch):
    data = _make_account_data(virtual_token_reserves=0, virtual_sol_reserves=30 * 10**9, complete=False)
    fake_client = FakeAsyncClient(FakeAccountInfoResp(FakeAccountInfoValue(data)))
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    client = PumpCurveOnChainClient("http://fake-rpc")
    price, complete, exists = asyncio.run(client.fetch_price_or_status(FAKE_MINT))

    assert price is None
    assert exists is True


def test_cuenta_mas_chica_de_lo_esperado_no_rompe(monkeypatch):
    fake_client = FakeAsyncClient(FakeAccountInfoResp(FakeAccountInfoValue(b"\x00" * 10)))
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    client = PumpCurveOnChainClient("http://fake-rpc")
    price, complete, exists = asyncio.run(client.fetch_price_or_status(FAKE_MINT))

    assert price is None
    assert complete is False
    assert exists is True


def test_excepcion_de_red_no_propaga(monkeypatch):
    class BoomClient:
        def __init__(self, url):
            pass

        async def __aenter__(self):
            raise ConnectionError("RPC caído")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(pump_module, "AsyncClient", BoomClient)

    client = PumpCurveOnChainClient("http://fake-rpc")
    price, complete, exists = asyncio.run(client.fetch_price_or_status(FAKE_MINT))

    assert price is None
    assert complete is False
    assert exists is False


def test_fetch_price_for_mint_devuelve_solo_el_precio(monkeypatch):
    data = _make_account_data(
        virtual_token_reserves=1_073_000_000 * 10**6,
        virtual_sol_reserves=30 * 10**9,
        complete=False,
    )
    fake_client = FakeAsyncClient(FakeAccountInfoResp(FakeAccountInfoValue(data)))
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    client = PumpCurveOnChainClient("http://fake-rpc")
    price = asyncio.run(client.fetch_price_for_mint(FAKE_MINT))

    assert abs(price - (30 / 1_073_000_000)) < 1e-12
