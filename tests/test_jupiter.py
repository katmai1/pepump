"""
Tests del cliente de Jupiter (jupiter.py) y de cómo se engancha en el resto
del bot: fuente de precio "jupiter" en OnchainFallback, entrada de un mint
que no está en ninguna fuente on-chain, y el executor mandando la orden
por Jupiter en vez de por la Lightning API.

Sin red: requests.get/post de Jupiter, el RPC y fetch_actual_fill están
reemplazados por dobles.
"""
import asyncio
import base64
import itertools
import time
from types import SimpleNamespace

import pytest
import requests
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from pepump import jupiter as jupiter_module
from pepump.bot import TrailingTakeProfitBot
from pepump.executor import TradeExecutor
from pepump.jupiter import (
    JUPITER_POOL,
    PRIORITY_PRICE,
    PRIORITY_TRADE,
    JupiterClient,
    JupiterError,
    _PriorityLimiter,
    build_jupiter_client,
    load_keypair,
)
from pepump.onchain import fallback as fallback_module
from pepump.onchain.programs import WSOL_MINT
from pepump.pumpportal import TradeOutcomeUnknown
from tests.conftest import FakeTradeStreamClient, SpyExecutor, make_config

MINT = "3w5cKpkfz2TAS22oXhMSZGjpsK6URL5d5db2thvwMtVx"


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


def _unsigned_tx_b64(payer: Keypair, other_signer: Keypair = None) -> str:
    """Transacción v0 sin nuestra firma, como la que devuelve /order. Con
    `other_signer`, lleva ya puesta la firma de un segundo firmante (como
    un market maker de JupiterZ)."""
    accounts = [AccountMeta(payer.pubkey(), True, True)]
    if other_signer is not None:
        accounts.append(AccountMeta(other_signer.pubkey(), True, False))
    ix = Instruction(Pubkey.new_unique(), b"", accounts)
    msg = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.default())
    signatures = [Signature.default()]
    if other_signer is not None:
        signatures.append(other_signer.sign_message(to_bytes_versioned(msg)))
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, signatures))).decode()


# --------------------------------------------------------------------------- #
# Clave privada
# --------------------------------------------------------------------------- #

def test_load_keypair_acepta_base58_y_lista_json():
    kp = Keypair()
    assert load_keypair(str(kp)).pubkey() == kp.pubkey()
    assert load_keypair(str(list(bytes(kp)))).pubkey() == kp.pubkey()


def test_load_keypair_invalida_no_muestra_la_clave():
    with pytest.raises(ValueError) as exc:
        load_keypair("esto-no-es-una-clave-secreta")
    assert "esto-no-es" not in str(exc.value)


def test_build_jupiter_client_sin_api_key_no_hay_jupiter():
    assert build_jupiter_client(make_config(wallet_private_key=str(Keypair()))) is None


def test_build_jupiter_client_rechaza_otra_wallet_que_la_de_pumpportal():
    cfg = make_config(jupiter_api_key="jup", wallet_address=str(Keypair().pubkey()),
                      wallet_private_key=str(Keypair()))
    with pytest.raises(ValueError, match="Tienen que ser la misma"):
        build_jupiter_client(cfg)


def test_build_jupiter_client_sin_clave_solo_cotiza():
    client = build_jupiter_client(make_config(jupiter_api_key="jup"))
    assert client is not None and not client.can_sign


def test_build_jupiter_client_con_clave_puede_firmar():
    kp = Keypair()
    client = build_jupiter_client(make_config(jupiter_api_key="jup", wallet_address=str(kp.pubkey()),
                                              wallet_private_key=str(kp)))
    assert client is not None and client.wallet_address == str(kp.pubkey())


def test_la_clave_privada_no_sale_en_el_repr_de_la_config():
    secret = str(Keypair())
    assert secret not in repr(make_config(wallet_private_key=secret))


# --------------------------------------------------------------------------- #
# Limitador de peticiones
# --------------------------------------------------------------------------- #

def test_limitador_respeta_el_intervalo_y_las_ordenes_adelantan_al_precio():
    async def run():
        limiter = _PriorityLimiter(0.15)
        order = []

        async def take(name, priority, delay=0.0):
            await asyncio.sleep(delay)
            await limiter.acquire(priority)
            order.append((name, time.monotonic()))

        await take("precio-1", PRIORITY_PRICE)
        # precio-2 llega antes que la orden, pero la orden pasa primero.
        await asyncio.gather(take("precio-2", PRIORITY_PRICE, 0.01), take("orden", PRIORITY_TRADE, 0.03))
        return order

    order = asyncio.run(run())
    assert [name for name, _ in order] == ["precio-1", "orden", "precio-2"]
    gaps = [b - a for (_, a), (_, b) in itertools.pairwise(order)]
    assert all(gap >= 0.14 for gap in gaps)


def test_limitador_libera_el_turno_si_se_cancela():
    async def run():
        limiter = _PriorityLimiter(0.05)
        await limiter.acquire(PRIORITY_PRICE)
        waiting = asyncio.create_task(limiter.acquire(PRIORITY_TRADE))
        await asyncio.sleep(0.01)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.wait_for(limiter.acquire(PRIORITY_PRICE), timeout=1)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Precio
# --------------------------------------------------------------------------- #

def _client(keypair=None) -> JupiterClient:
    client = JupiterClient("jup-key", keypair, request_interval_seconds=0.0)

    async def decimals(mint, rpc_url):
        return 6
    client.token_decimals = decimals
    return client


def test_sell_price_es_el_sol_recibido_por_token(monkeypatch):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        return FakeResponse(body={"outAmount": "500000000"})  # 0.5 SOL
    monkeypatch.setattr(requests, "get", fake_get)

    price = asyncio.run(_client().sell_price(MINT, 1000.0, "http://rpc"))

    assert price == pytest.approx(0.0005)
    url, params, headers = calls[0]
    assert url.endswith("/swap/v2/order")
    assert params == {"inputMint": MINT, "outputMint": WSOL_MINT, "amount": "1000000000"}
    assert "taker" not in params
    assert headers == {"x-api-key": "jup-key"}


def test_sin_ruta_es_un_error_claro(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(body={"outAmount": "0",
                                                                            "errorMessage": "No route"}))
    with pytest.raises(JupiterError, match="No route"):
        asyncio.run(_client().buy_tokens(MINT, 0.05, "http://rpc"))


def test_una_orden_reintenta_tras_un_429(monkeypatch):
    responses = [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(body={"outAmount": "7"})]
    monkeypatch.setattr(requests, "get", lambda *a, **k: responses.pop(0))

    data = asyncio.run(_client().quote(WSOL_MINT, MINT, 1, priority=PRIORITY_TRADE))

    assert data["outAmount"] == "7"


def test_una_consulta_de_precio_no_reintenta_un_429(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(429, headers={"Retry-After": "0"}))
    with pytest.raises(JupiterError, match="429"):
        asyncio.run(_client().quote(WSOL_MINT, MINT, 1))


# --------------------------------------------------------------------------- #
# Órdenes
# --------------------------------------------------------------------------- #

def test_firma_en_nuestro_hueco_sin_pisar_la_del_market_maker():
    kp, maker = Keypair(), Keypair()
    signed = VersionedTransaction.from_bytes(
        base64.b64decode(_client(kp)._sign(_unsigned_tx_b64(kp, maker))))

    message = to_bytes_versioned(signed.message)
    assert signed.signatures[0].verify(kp.pubkey(), message)
    assert signed.signatures[1].verify(maker.pubkey(), message)


def test_no_firma_una_tx_que_no_pide_nuestra_firma():
    with pytest.raises(JupiterError):
        _client(Keypair())._sign(_unsigned_tx_b64(Keypair()))


def _fake_trade(monkeypatch, kp, execute_body=None, execute_exc=None, fill=None):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["order"] = params
        return FakeResponse(body={"transaction": _unsigned_tx_b64(kp), "requestId": "req-1",
                                  "outAmount": "1", "router": "metis", "feeBps": 10})

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["execute"] = (url, json)
        if execute_exc is not None:
            raise execute_exc
        return FakeResponse(body=execute_body if execute_body is not None
                            else {"status": "Success", "code": 0, "signature": "SIG"})

    async def fake_fill(signature, rpc_url, mint, wallet=None):
        seen["fill"] = (signature, mint, wallet)
        return fill

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(jupiter_module, "fetch_actual_fill", fake_fill)
    return seen


def test_compra_por_jupiter_firma_ejecuta_y_lee_el_fill(monkeypatch):
    kp = Keypair()
    seen = _fake_trade(monkeypatch, kp, fill={"sol_delta": -0.0503, "token_delta": 1234.5, "wallet": "x"})

    result = asyncio.run(_client(kp).execute_trade(
        action="buy", mint=MINT, amount=0.05, slippage=15, priority_fee=0.00001,
        solana_rpc_url="http://rpc", denominated_in_sol=True))

    assert seen["order"] == {"inputMint": WSOL_MINT, "outputMint": MINT, "amount": "50000000",
                             "taker": str(kp.pubkey()), "slippageBps": 1500,
                             "priorityFeeLamports": 10000, "broadcastFeeType": "maxCap"}
    url, body = seen["execute"]
    assert url.endswith("/swap/v2/execute") and body["requestId"] == "req-1"
    signed = VersionedTransaction.from_bytes(base64.b64decode(body["signedTransaction"]))
    assert signed.signatures[0].verify(kp.pubkey(), to_bytes_versioned(signed.message))
    assert seen["fill"] == ("SIG", MINT, str(kp.pubkey()))
    assert result["actual_sol_delta"] == -0.0503 and result["actual_token_delta"] == 1234.5


def test_venta_por_jupiter_vende_todo_el_saldo(monkeypatch):
    kp = Keypair()
    seen = _fake_trade(monkeypatch, kp)
    client = _client(kp)

    async def balance(mint, rpc_url):
        return 987654
    client.wallet_token_raw_balance = balance

    asyncio.run(client.execute_trade(action="sell", mint=MINT, amount="100%", slippage=25,
                                     priority_fee=0.00004, solana_rpc_url="http://rpc"))

    assert seen["order"]["inputMint"] == MINT and seen["order"]["outputMint"] == WSOL_MINT
    assert seen["order"]["amount"] == "987654"


def test_venta_sin_tokens_en_la_wallet_pide_mirar_la_wallet(monkeypatch):
    client = _client(Keypair())

    async def balance(mint, rpc_url):
        return 0
    client.wallet_token_raw_balance = balance

    with pytest.raises(TradeOutcomeUnknown):
        asyncio.run(client.execute_trade(action="sell", mint=MINT, amount="100%", slippage=25,
                                         priority_fee=0.0, solana_rpc_url="http://rpc"))


def test_sin_clave_no_se_puede_operar_en_real():
    with pytest.raises(RuntimeError, match="PEPUMP_WALLET_PRIVATE_KEY"):
        asyncio.run(_client().execute_trade(action="buy", mint=MINT, amount=0.05, slippage=15,
                                            priority_fee=0.0, solana_rpc_url="http://rpc"))


@pytest.mark.parametrize("body, exc", [
    ({"status": "Failed", "code": -1000, "error": "Failed to land", "signature": "S"}, RuntimeError),
    ({"status": "Failed", "code": -1001, "error": "Unknown error"}, TradeOutcomeUnknown),
])
def test_execute_fallido(monkeypatch, body, exc):
    kp = Keypair()
    _fake_trade(monkeypatch, kp, execute_body=body)
    with pytest.raises(exc) as info:
        asyncio.run(_client(kp).execute_trade(action="buy", mint=MINT, amount=0.05, slippage=15,
                                              priority_fee=0.0, solana_rpc_url="http://rpc"))
    if exc is RuntimeError:
        assert not isinstance(info.value, TradeOutcomeUnknown)


def test_execute_sin_respuesta_es_resultado_desconocido(monkeypatch):
    kp = Keypair()
    _fake_trade(monkeypatch, kp, execute_exc=requests.exceptions.ReadTimeout("timeout"))
    with pytest.raises(TradeOutcomeUnknown):
        asyncio.run(_client(kp).execute_trade(action="buy", mint=MINT, amount=0.05, slippage=15,
                                              priority_fee=0.0, solana_rpc_url="http://rpc"))


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #

class FakeJupiter:
    def __init__(self, tokens=1000.0, price=0.00004, can_sign=True, fail_quote=False):
        self.tokens = tokens
        self.price = price
        self.can_sign = can_sign
        self.fail_quote = fail_quote
        self.trades = []
        self.sell_quotes = []

    async def buy_tokens(self, mint, sol, rpc_url, priority=PRIORITY_PRICE):
        if self.fail_quote:
            raise JupiterError("sin ruta (fake)")
        return self.tokens

    async def sell_price(self, mint, token_amount, rpc_url, priority=PRIORITY_PRICE):
        if self.fail_quote:
            raise JupiterError("sin ruta (fake)")
        self.sell_quotes.append((token_amount, priority))
        return self.price

    async def execute_trade(self, **kwargs):
        self.trades.append(kwargs)
        if kwargs["action"] == "buy":
            return {"signature": "S", "actual_sol_delta": -0.0503, "actual_token_delta": 1200.0}
        return {"signature": "S", "actual_sol_delta": 0.06, "actual_token_delta": -1200.0}


class LightningQueNoSeUsa:
    async def execute_lightning_trade(self, **kwargs):
        raise AssertionError("la orden tenía que ir por Jupiter")


def test_executor_real_manda_la_orden_por_jupiter(tmp_path):
    jupiter = FakeJupiter()
    cfg = make_config(live=True, trade_history_csv=str(tmp_path / "h.csv"))
    executor = TradeExecutor(client=LightningQueNoSeUsa(), live=True, config=cfg, jupiter=jupiter)

    pos = asyncio.run(executor.buy(MINT, 0.00004, pool_override=JUPITER_POOL))
    asyncio.run(executor.sell(pos, 0.00005, "trailing", pool_override=JUPITER_POOL))

    assert [t["action"] for t in jupiter.trades] == ["buy", "sell"]
    assert pos.entry_is_real_fill and pos.token_amount == 1200.0
    assert pos.realized_pnl_sol == pytest.approx(0.06 - 0.0503)


def test_executor_simulado_compra_con_la_cotizacion_de_jupiter():
    cfg = make_config(sim_costs=True, sim_slippage_pct=1.0, sim_fee_pct=2.0, buy_sol=0.05)
    executor = TradeExecutor(client=None, live=False, config=cfg, jupiter=FakeJupiter(tokens=1000.0))

    pos = asyncio.run(executor.buy(MINT, 0.00004, pool_override=JUPITER_POOL))

    # Los tokens salen de la cotización (menos el slippage simulado), no
    # de buy_sol / precio con sim_fee_pct encima.
    assert pos.token_amount == pytest.approx(990.0)
    assert pos.market_entry_price == 0.00004
    assert pos.sol_amount > 0.05


def test_executor_simulado_vende_a_lo_que_cotiza_jupiter(tmp_path):
    cfg = make_config(sim_costs=True, sim_slippage_pct=0.0, sim_fee_pct=2.0, priority_fee=0.0,
                      trade_history_csv=str(tmp_path / "h.csv"))
    executor = TradeExecutor(client=None, live=False, config=cfg, jupiter=FakeJupiter(tokens=1000.0))
    pos = asyncio.run(executor.buy(MINT, 0.00004, pool_override=JUPITER_POOL))

    asyncio.run(executor.sell(pos, 0.0001, "trailing", pool_override=JUPITER_POOL))

    # Lo que cotiza Jupiter por los 1000 tokens (0.00004 x 1000 = 0.04 SOL),
    # no el precio de mercado (0.0001), y sin el 2% de sim_fee_pct encima.
    from pepump.executor import NETWORK_FEE_SOL, TOKEN_ACCOUNT_RENT_SOL
    assert pos.proceeds_sol == pytest.approx(0.04 - NETWORK_FEE_SOL + TOKEN_ACCOUNT_RENT_SOL)
    assert pos.close_reason == "trailing"


def test_executor_simulado_si_jupiter_no_cotiza_la_venta_cobra_las_comisiones(tmp_path):
    cfg = make_config(sim_costs=True, sim_slippage_pct=0.0, sim_fee_pct=2.0, priority_fee=0.0,
                      trade_history_csv=str(tmp_path / "h.csv"))
    jupiter = FakeJupiter(tokens=1000.0)
    executor = TradeExecutor(client=None, live=False, config=cfg, jupiter=jupiter)
    pos = asyncio.run(executor.buy(MINT, 0.00004, pool_override=JUPITER_POOL))
    jupiter.fail_quote = True

    asyncio.run(executor.sell(pos, 0.0001, "trailing", pool_override=JUPITER_POOL))

    from pepump.executor import NETWORK_FEE_SOL, TOKEN_ACCOUNT_RENT_SOL
    assert pos.proceeds_sol == pytest.approx(0.1 * 0.98 - NETWORK_FEE_SOL + TOKEN_ACCOUNT_RENT_SOL)


# --------------------------------------------------------------------------- #
# Entrada de un mint que solo se puede operar por Jupiter
# --------------------------------------------------------------------------- #

class _SinNada:
    def __init__(self, rpc_url):
        pass

    async def fetch_price_or_confirm_absent(self, mint):
        return None, True

    async def fetch_price_or_status(self, mint):
        return None, False, False


async def _ack_then_hang():
    yield {"message": "Successfully subscribed to keys."}
    await asyncio.Future()
    yield {}  # pragma: no cover


@pytest.fixture
def _mint_fuera_de_todo(monkeypatch):
    monkeypatch.setattr(fallback_module, "PumpSwapOnChainClient", _SinNada)
    monkeypatch.setattr(fallback_module, "PumpCurveOnChainClient", _SinNada)


def _bot(cfg, jupiter):
    bot = TrailingTakeProfitBot(client=FakeTradeStreamClient(), executor=SpyExecutor(), config=cfg,
                                jupiter=jupiter)
    bot._stream.events = _ack_then_hang()
    return bot


def test_mint_fuera_de_las_fuentes_on_chain_entra_por_jupiter(_mint_fuera_de_todo):
    jupiter = FakeJupiter(tokens=1250.0, price=0.00004)
    bot = _bot(make_config(live_feed_timeout_seconds=5.0), jupiter)

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price == 0.00004
    assert bot.onchain.source == "jupiter"
    assert bot.onchain.pool_override() == JUPITER_POOL
    # Se cotiza la venta de lo que se compraría con buy_sol.
    assert jupiter.sell_quotes == [(1250.0, PRIORITY_PRICE)]


def test_en_real_sin_clave_no_entra_por_jupiter(_mint_fuera_de_todo, caplog):
    bot = _bot(make_config(live=True, live_feed_timeout_seconds=5.0), FakeJupiter(can_sign=False))

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price is None
    assert bot.onchain.source is None
    assert "wallet_private_key" in caplog.text


def test_sin_ruta_en_jupiter_aborta_como_antes(_mint_fuera_de_todo):
    bot = _bot(make_config(live_feed_timeout_seconds=5.0), FakeJupiter(fail_quote=True))

    price = asyncio.run(asyncio.wait_for(bot._get_reference_price(), timeout=5))

    assert price is None
    assert bot.onchain.source is None


def test_tras_comprar_se_cotiza_la_cantidad_real_de_la_posicion():
    jupiter = FakeJupiter()
    bot = _bot(make_config(), jupiter)
    bot.onchain.source = "jupiter"

    async def run():
        await bot._on_first_price(0.00004)
        return await bot.onchain.fetch_price()

    asyncio.run(run())

    assert bot.executor.last_buy_pool_override == JUPITER_POOL
    assert jupiter.sell_quotes[-1][0] == pytest.approx(bot.position.token_amount)


def test_posicion_retomada_cotiza_sus_tokens():
    from pepump.executor import Position
    position = Position(mint=MINT, entry_price=0.00004, sol_amount=0.05, token_amount=1234.0)
    bot = TrailingTakeProfitBot(client=FakeTradeStreamClient(), executor=SpyExecutor(), config=make_config(),
                                resume_position=position, resume_onchain_source="jupiter",
                                jupiter=FakeJupiter())

    assert bot.onchain.jupiter_tokens == 1234.0
    assert asyncio.run(bot.onchain.fetch_price()) == 0.00004


def test_retomada_por_jupiter_sin_api_key_avisa():
    from pepump.onchain.fallback import OnchainFallback
    onchain = OnchainFallback(MINT, "http://rpc", "jupiter")
    onchain.jupiter_tokens = 10.0
    with pytest.raises(RuntimeError, match="jupiter_api_key"):
        asyncio.run(onchain.fetch_price())


def test_vista_previa_ofrece_jupiter(monkeypatch):
    from pepump import manager as manager_module
    from pepump.manager import PositionManager
    monkeypatch.setattr(manager_module, "PumpCurveOnChainClient", _SinNada)
    monkeypatch.setattr(manager_module, "PumpSwapOnChainClient", _SinNada)
    manager = PositionManager(base_config=make_config(), client=SimpleNamespace(), feed=object(),
                              jupiter=FakeJupiter(price=0.00004))

    preview = asyncio.run(manager.preview(MINT))

    assert preview["market"] == "jupiter" and preview["price"] == 0.00004
