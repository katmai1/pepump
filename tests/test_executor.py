import pytest

from pepump.executor import Position, TradeExecutor


def test_position_highest_price_starts_at_entry_price():
    pos = Position(mint="M", entry_price=2.0, sol_amount=0.05, token_amount=25.0)
    assert pos.highest_price == 2.0
    assert pos.closed is False
    assert pos.armed is False


@pytest.mark.parametrize("entry, price, expected_pct", [
    (1.0, 1.5, 50.0),
    (1.0, 0.5, -50.0),
    (2.0, 2.0, 0.0),
])
def test_position_pnl_pct(entry, price, expected_pct):
    pos = Position(mint="M", entry_price=entry, sol_amount=0.05, token_amount=0.05 / entry)
    assert pos.pnl_pct(price) == pytest.approx(expected_pct)


class DummyConfig:
    buy_sol = 0.05
    slippage = 15.0
    priority_fee = 0.00001
    pool = "auto"
    solana_rpc_url = "http://fake"
    tx_confirm_timeout_seconds = 30.0
    tx_confirm_poll_interval_seconds = 2.0


class FakeLightningClient:
    def __init__(self, result=None, exc=None):
        self.result = result or {"signature": "fake-sig"}
        self.exc = exc
        self.calls = []

    async def execute_lightning_trade(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result


async def test_buy_simulated_does_not_touch_network_and_returns_position():
    client = FakeLightningClient()
    executor = TradeExecutor(client=client, live=False, config=DummyConfig())

    pos = await executor.buy("MintABC", price=2.0)

    assert client.calls == []  # modo simulado: no debe llamar a la Lightning API
    assert pos.mint == "MintABC"
    assert pos.entry_price == 2.0
    assert pos.token_amount == pytest.approx(0.05 / 2.0)
    assert pos.closed is False


async def test_sell_simulated_marks_position_closed():
    client = FakeLightningClient()
    executor = TradeExecutor(client=client, live=False, config=DummyConfig())
    pos = Position(mint="M", entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    await executor.sell(pos, price=1.2, reason="test")

    assert client.calls == []
    assert pos.closed is True


async def test_buy_live_success_calls_lightning_api_and_returns_position():
    client = FakeLightningClient(result={"signature": "abc"})
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())

    pos = await executor.buy("MintABC", price=1.0)

    assert len(client.calls) == 1
    assert client.calls[0]["action"] == "buy"
    assert client.calls[0]["mint"] == "MintABC"
    assert pos.entry_price == 1.0


async def test_buy_live_failure_raises_and_no_position_returned():
    client = FakeLightningClient(exc=RuntimeError("tx falló on-chain"))
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())

    with pytest.raises(RuntimeError, match="tx falló"):
        await executor.buy("MintABC", price=1.0)


async def test_sell_live_success_marks_closed():
    client = FakeLightningClient(result={"signature": "xyz"})
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())
    pos = Position(mint="M", entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    await executor.sell(pos, price=1.3, reason="trailing-stop")

    assert pos.closed is True
    assert client.calls[0]["action"] == "sell"
    assert client.calls[0]["amount"] == "100%"


async def test_sell_live_failure_raises_and_position_stays_open():
    """
    Regresión sobre el BUGFIX ya presente en el código: si la venta real
    falla (Lightning API rechaza la orden, o la tx confirma pero falla
    on-chain), la excepción debe propagarse y la posición NUNCA debe
    quedar marcada como cerrada -si no, el bot "cree" que vendió con SOL
    real y deja de vigilar una posición que en realidad sigue abierta.
    """
    client = FakeLightningClient(exc=RuntimeError("slippage excedido"))
    executor = TradeExecutor(client=client, live=True, config=DummyConfig())
    pos = Position(mint="M", entry_price=1.0, sol_amount=0.05, token_amount=0.05)

    with pytest.raises(RuntimeError, match="slippage excedido"):
        await executor.sell(pos, price=1.3, reason="trailing-stop")

    assert pos.closed is False
