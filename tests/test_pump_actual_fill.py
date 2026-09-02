"""
Tests para pump.py:_fetch_actual_fill, que lee los balances REALES
pre/post de una transacción de compra/venta ya confirmada para poder
reportar cuánto SOL y cuántos tokens se movieron de verdad -en vez de
la estimación a partir del precio de referencia que se usaba antes.

No pegan a ningún RPC real: reemplazan `AsyncClient` (tal como lo
importa pump.py) por un doble mínimo que devuelve una respuesta fija
con la misma forma (por duck typing) que la que devuelve solana-py/
solders para `get_transaction`.
"""
import asyncio

import pytest
from solders.signature import Signature

from pepump import pump as pump_module


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Los tests de reintento ejercitan asyncio.sleep entre intentos;
    lo pisamos para que no frenen la suite de verdad."""
    async def _fake_sleep(seconds):
        return None
    monkeypatch.setattr(pump_module.asyncio, "sleep", _fake_sleep)

# Firma con formato válido (Signature.from_string no debe explotar),
# no corresponde a ninguna tx real -no hace falta, get_transaction está
# mockeado.
FAKE_SIGNATURE = str(Signature.default())

WALLET = "Wa11etPubkey1111111111111111111111111111111"
OTHER_ACCOUNT = "Other11111111111111111111111111111111111111"
MINT = "TargetMint111111111111111111111111111111111"


class FakeUiTokenAmount:
    def __init__(self, ui_amount):
        self.ui_amount = ui_amount


class FakeTokenBalance:
    def __init__(self, mint, owner, ui_amount):
        self.mint = mint
        self.owner = owner
        self.ui_token_amount = FakeUiTokenAmount(ui_amount)


class FakeMeta:
    def __init__(self, pre_balances, post_balances, pre_token_balances, post_token_balances):
        self.pre_balances = pre_balances
        self.post_balances = post_balances
        self.pre_token_balances = pre_token_balances
        self.post_token_balances = post_token_balances


class FakeMessage:
    def __init__(self, account_keys):
        self.account_keys = account_keys


class FakeInnerTransaction:
    def __init__(self, account_keys):
        self.message = FakeMessage(account_keys)


class FakeEncodedTransactionWithStatusMeta:
    def __init__(self, meta, account_keys):
        self.meta = meta
        self.transaction = FakeInnerTransaction(account_keys)


class FakeConfirmedTx:
    def __init__(self, meta, account_keys):
        self.transaction = FakeEncodedTransactionWithStatusMeta(meta, account_keys)


class FakeGetTransactionResp:
    def __init__(self, value):
        self.value = value


class FakeAsyncClient:
    """Reemplaza solana.rpc.async_api.AsyncClient dentro de pump.py:
    solo implementa el método y el protocolo async-context-manager que
    usa _fetch_actual_fill, devolviendo la respuesta fija que se le
    pase."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __init__(self, resp=None, exc=None, fail_times=0):
        self._resp = resp
        self._exc = exc
        # Cuántas veces devolver "todavía no encontrada" (resp.value=None)
        # antes de servir self._resp -simula la tx no indexada aún.
        self._fail_times = fail_times
        self.calls = 0

    async def get_transaction(self, sig, encoding=None, commitment=None,
                               max_supported_transaction_version=None):
        self.calls += 1
        if self._exc:
            raise self._exc
        if self.calls <= self._fail_times:
            return FakeGetTransactionResp(None)
        return self._resp


def _make_resp(sol_delta_lamports: int, pre_tokens, post_tokens):
    """Arma una respuesta fake de get_transaction para la wallet (índice
    0 de account_keys, como el fee payer real) con el delta de SOL/
    tokens dado. `pre_tokens`/`post_tokens` en None simula que el mint
    no aparece en ese lado (p. ej. la wallet nunca tuvo el token antes
    de la primera compra)."""
    pre_token_balances = [FakeTokenBalance(MINT, WALLET, pre_tokens)] if pre_tokens is not None else []
    post_token_balances = [FakeTokenBalance(MINT, WALLET, post_tokens)] if post_tokens is not None else []
    meta = FakeMeta(
        pre_balances=[1_000_000_000, 5_000_000_000],
        post_balances=[1_000_000_000 + sol_delta_lamports, 5_000_000_000],
        pre_token_balances=pre_token_balances,
        post_token_balances=post_token_balances,
    )
    return FakeGetTransactionResp(FakeConfirmedTx(meta, [WALLET, OTHER_ACCOUNT]))


def test_fetch_actual_fill_de_una_compra_sol_baja_tokens_suben(monkeypatch):
    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=0.0, post_tokens=1_234.5)
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(pump_module._fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is not None
    assert fill["sol_delta"] == pytest.approx(-0.0505)   # gastamos 0.0505 SOL (fees incluidos)
    assert fill["token_delta"] == pytest.approx(1234.5)  # recibimos 1234.5 tokens


def test_fetch_actual_fill_de_una_venta_sol_sube_tokens_bajan(monkeypatch):
    resp = _make_resp(sol_delta_lamports=48_000_000, pre_tokens=1_234.5, post_tokens=0.0)
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(pump_module._fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is not None
    assert fill["sol_delta"] == pytest.approx(0.048)       # recibimos 0.048 SOL (netos de fees)
    assert fill["token_delta"] == pytest.approx(-1234.5)   # vendimos las 1234.5 tokens


def test_fetch_actual_fill_wallet_sin_balance_previo_del_token(monkeypatch):
    """Primera compra de un mint: la wallet no tiene ninguna token
    account con ese mint todavía, así que no aparece en
    pre_token_balances -pre_tokens debe tratarse como 0, no como
    'no se pudo leer'."""
    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=None, post_tokens=1_234.5)
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(pump_module._fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is not None
    assert fill["token_delta"] == pytest.approx(1234.5)


def test_fetch_actual_fill_none_si_no_hay_meta(monkeypatch):
    resp = FakeGetTransactionResp(None)
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(pump_module._fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is None


def test_fetch_actual_fill_none_si_get_transaction_explota(monkeypatch):
    monkeypatch.setattr(
        pump_module, "AsyncClient",
        lambda url: FakeAsyncClient(exc=RuntimeError("RPC caído")),
    )

    fill = asyncio.run(pump_module._fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is None


def test_fetch_actual_fill_none_si_mint_no_aparece_en_ningun_lado(monkeypatch):
    """Si el mint no está ni en pre ni en post token balances (formato
    de respuesta inesperado, o mint equivocado), no hay que inventar un
    delta de 0 -mejor devolver None y que el llamador caiga al
    estimado."""
    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=None, post_tokens=None)
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(pump_module._fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is None


def test_fetch_actual_fill_reintenta_si_la_tx_todavia_no_esta_indexada(monkeypatch):
    """Caso real reportado: get_transaction devuelve resp.value=None las
    primeras veces (la tx confirmó pero el nodo RPC todavía no puede
    servirla) y recién en el último intento aparece -_fetch_actual_fill
    debe reintentar y devolver los datos reales, no rendirse de una."""
    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=0.0, post_tokens=1_234.5)
    fake_client = FakeAsyncClient(resp=resp, fail_times=2)
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    fill = asyncio.run(pump_module._fetch_actual_fill(
        FAKE_SIGNATURE, "http://fake-rpc", MINT, max_attempts=3, retry_delay_seconds=0
    ))

    assert fill is not None
    assert fill["token_delta"] == pytest.approx(1234.5)
    assert fake_client.calls == 3


def test_fetch_actual_fill_none_si_la_tx_nunca_aparece_tras_los_reintentos(monkeypatch):
    """Si se agotan los intentos y la tx sigue sin poder leerse, cae
    limpiamente a None (el llamador usa el estimado) en vez de colgarse
    reintentando para siempre."""
    fake_client = FakeAsyncClient(resp=None, fail_times=99)  # nunca "aparece"
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    fill = asyncio.run(pump_module._fetch_actual_fill(
        FAKE_SIGNATURE, "http://fake-rpc", MINT, max_attempts=3, retry_delay_seconds=0
    ))

    assert fill is None
    assert fake_client.calls == 3


def test_fetch_actual_fill_pasa_commitment_confirmed(monkeypatch):
    """get_transaction debe pedirse explícitamente con commitment=Confirmed
    -no depender del default del cliente (finalized), que tarda más y
    empeora el desfasaje con _confirm_transaction_onchain."""
    from solana.rpc.commitment import Confirmed

    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=0.0, post_tokens=1_234.5)
    seen_commitments = []

    class RecordingFakeAsyncClient(FakeAsyncClient):
        async def get_transaction(self, sig, encoding=None, commitment=None,
                                   max_supported_transaction_version=None):
            seen_commitments.append(commitment)
            return await super().get_transaction(
                sig, encoding=encoding, commitment=commitment,
                max_supported_transaction_version=max_supported_transaction_version,
            )

    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: RecordingFakeAsyncClient(resp=resp))

    asyncio.run(pump_module._fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert seen_commitments == [Confirmed]
