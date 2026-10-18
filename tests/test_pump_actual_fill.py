"""
Tests para onchain/txinfo.py:fetch_actual_fill, que lee los balances REALES
pre/post de una transacción de compra/venta ya confirmada para poder
reportar cuánto SOL y cuántos tokens se movieron de verdad -en vez de
la estimación a partir del precio de referencia que se usaba antes.

No pegan a ningún RPC real: reemplazan `AsyncClient` (tal como lo
importa txinfo.py) por un doble mínimo que devuelve una respuesta fija
con la misma forma (por duck typing) que la que devuelve solana-py/
solders para `get_transaction`.
"""
import asyncio

import pytest
from solders.signature import Signature

from pepump.onchain import txinfo as txinfo_module


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Los tests de reintento ejercitan asyncio.sleep entre intentos;
    lo pisamos para que no frenen la suite de verdad."""
    async def _fake_sleep(seconds):
        return None
    monkeypatch.setattr(txinfo_module.asyncio, "sleep", _fake_sleep)

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
    def __init__(self, mint, owner, ui_amount, account_index=None):
        self.mint = mint
        self.owner = owner
        self.ui_token_amount = FakeUiTokenAmount(ui_amount)
        self.account_index = account_index


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
    """Reemplaza solana.rpc.async_api.AsyncClient dentro de txinfo.py:
    solo implementa el método y el protocolo async-context-manager que
    usa fetch_actual_fill, devolviendo la respuesta fija que se le
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
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is not None
    assert fill["sol_delta"] == pytest.approx(-0.0505)   # gastamos 0.0505 SOL (fees incluidos)
    assert fill["token_delta"] == pytest.approx(1234.5)  # recibimos 1234.5 tokens


def test_fetch_actual_fill_de_una_venta_sol_sube_tokens_bajan(monkeypatch):
    resp = _make_resp(sol_delta_lamports=48_000_000, pre_tokens=1_234.5, post_tokens=0.0)
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is not None
    assert fill["sol_delta"] == pytest.approx(0.048)       # recibimos 0.048 SOL (netos de fees)
    assert fill["token_delta"] == pytest.approx(-1234.5)   # vendimos las 1234.5 tokens


def test_fetch_actual_fill_wallet_sin_balance_previo_del_token(monkeypatch):
    """Primera compra de un mint: la wallet no tiene ninguna token
    account con ese mint todavía, así que no aparece en
    pre_token_balances -pre_tokens debe tratarse como 0, no como
    'no se pudo leer'."""
    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=None, post_tokens=1_234.5)
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is not None
    assert fill["token_delta"] == pytest.approx(1234.5)


def test_fetch_actual_fill_none_si_no_hay_meta(monkeypatch):
    resp = FakeGetTransactionResp(None)
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is None


def test_fetch_actual_fill_none_si_get_transaction_explota(monkeypatch):
    monkeypatch.setattr(
        txinfo_module, "AsyncClient",
        lambda url: FakeAsyncClient(exc=RuntimeError("RPC caído")),
    )

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is None


def test_fetch_actual_fill_none_si_mint_no_aparece_en_ningun_lado(monkeypatch):
    """Si el mint no está ni en pre ni en post token balances (formato
    de respuesta inesperado, o mint equivocado), no hay que inventar un
    delta de 0 -mejor devolver None y que el llamador caiga al
    estimado."""
    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=None, post_tokens=None)
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert fill is None


def test_fetch_actual_fill_reintenta_si_la_tx_todavia_no_esta_indexada(monkeypatch):
    """Caso real reportado: get_transaction devuelve resp.value=None las
    primeras veces (la tx confirmó pero el nodo RPC todavía no puede
    servirla) y solo en el último intento aparece -fetch_actual_fill
    debe reintentar y devolver los datos reales, no rendirse de inmediato."""
    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=0.0, post_tokens=1_234.5)
    fake_client = FakeAsyncClient(resp=resp, fail_times=2)
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: fake_client)

    fill = asyncio.run(txinfo_module.fetch_actual_fill(
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
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: fake_client)

    fill = asyncio.run(txinfo_module.fetch_actual_fill(
        FAKE_SIGNATURE, "http://fake-rpc", MINT, max_attempts=3, retry_delay_seconds=0
    ))

    assert fill is None
    assert fake_client.calls == 3


def test_fetch_actual_fill_pasa_commitment_confirmed(monkeypatch):
    """get_transaction debe pedirse explícitamente con commitment=Confirmed
    -no depender del default del cliente (finalized), que tarda más y
    empeora el desfase con _confirm_transaction_onchain."""
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

    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: RecordingFakeAsyncClient(resp=resp))

    asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))

    assert seen_commitments == [Confirmed]


# --------------------------------------------------------------------------- #
# BUGFIX: solders devuelve mint/owner como Pubkey, no como str
# --------------------------------------------------------------------------- #
#
# El resto de esta suite usa dobles con strings, que es justamente por lo
# que el bug pasó desapercibido: contra un RPC real la comparación
# `b.mint == mint` (str) daba False SIEMPRE, así que fetch_actual_fill
# devolvía None en cada compra y en cada venta y el bot caía al valor
# ESTIMADO todas las veces. Estos tests usan los tipos REALES.

from solders.pubkey import Pubkey  # noqa: E402

REAL_MINT = "So11111111111111111111111111111111111111112"
REAL_WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


class PubkeyTokenBalance:
    """Como FakeTokenBalance, pero con mint/owner tipados igual que los
    devuelve solders de verdad."""

    def __init__(self, mint, owner, ui_amount):
        self.mint = Pubkey.from_string(mint)
        self.owner = Pubkey.from_string(owner) if owner is not None else None
        self.ui_token_amount = FakeUiTokenAmount(ui_amount)


def make_fake_async_client(tx):
    """Igual que en los tests de arriba, pero devolviendo el FakeConfirmedTx
    que se le pase envuelto en la respuesta de get_transaction."""
    resp = FakeGetTransactionResp(tx)
    return lambda url: FakeAsyncClient(resp=resp)


def _tx_con_tipos_reales(pre_tokens, post_tokens, owner=REAL_WALLET):
    meta = FakeMeta(
        pre_balances=[2_000_000_000, 0],
        post_balances=[1_900_000_000, 0],
        pre_token_balances=([PubkeyTokenBalance(REAL_MINT, owner, pre_tokens)]
                            if pre_tokens is not None else []),
        post_token_balances=([PubkeyTokenBalance(REAL_MINT, owner, post_tokens)]
                             if post_tokens is not None else []),
    )
    return FakeConfirmedTx(meta, [Pubkey.from_string(REAL_WALLET), Pubkey.from_string(REAL_MINT)])


def test_fetch_actual_fill_funciona_con_los_tipos_reales_de_solders(monkeypatch):
    """El test de regresión del bug: con Pubkey (no str) tiene que leer
    el fill de todos modos. Antes devolvía None y el bot usaba el estimado."""
    tx = _tx_con_tipos_reales(pre_tokens=None, post_tokens=1500.0)
    monkeypatch.setattr(txinfo_module, "AsyncClient", make_fake_async_client(tx))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "https://fake-rpc.test", REAL_MINT))

    assert fill is not None
    assert fill["token_delta"] == pytest.approx(1500.0)
    assert fill["sol_delta"] == pytest.approx(-0.1)


def test_fetch_actual_fill_tolera_owner_ausente_si_hay_un_solo_balance(monkeypatch):
    """Algunos nodos no mandan `owner` en los token balances. Si hay UNA
    sola entrada para el mint, es la nuestra: descartar el fill real por
    un campo opcional que el nodo omitió sería descartar el dato bueno."""
    tx = _tx_con_tipos_reales(pre_tokens=None, post_tokens=1500.0, owner=None)
    monkeypatch.setattr(txinfo_module, "AsyncClient", make_fake_async_client(tx))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "https://fake-rpc.test", REAL_MINT))

    assert fill is not None
    assert fill["token_delta"] == pytest.approx(1500.0)


def test_fetch_actual_fill_no_adivina_si_hay_varias_cuentas_sin_owner(monkeypatch):
    """Con varias cuentas del mismo mint y ninguna atribuible a la wallet,
    mejor caer al estimado que elegir una al azar."""
    meta = FakeMeta(
        pre_balances=[2_000_000_000, 0],
        post_balances=[1_900_000_000, 0],
        pre_token_balances=[],
        post_token_balances=[
            PubkeyTokenBalance(REAL_MINT, None, 1500.0),
            PubkeyTokenBalance(REAL_MINT, None, 99.0),
        ],
    )
    tx = FakeConfirmedTx(meta, [Pubkey.from_string(REAL_WALLET)])
    monkeypatch.setattr(txinfo_module, "AsyncClient", make_fake_async_client(tx))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "https://fake-rpc.test", REAL_MINT))

    assert fill is None


# --------------------------------------------------------------------------- #
# BUGFIX: la token account de la bonding curve se tomaba como propia
# --------------------------------------------------------------------------- #
#
# Éste es el bug por el que las COMPRAS caían siempre al estimado aunque la
# tx confirmara: en una compra de pump.fun, `pre_token_balances` trae la
# token account de la BONDING CURVE (que es la que tiene los tokens) y NO
# trae ninguna cuenta nuestra -nuestra ATA se crea en esa misma tx-. Como
# era la única entrada de ese mint, se la tomaba como propia y el
# token_delta salía enorme y NEGATIVO, que es justo lo que executor.buy
# descarta. En las ventas no pasaba, porque ahí nuestra ATA ya existe en
# los dos lados: de ahí que la venta sí mostrara datos reales.

CURVE_ACCOUNT_OWNER = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # programa de pump.fun


def _tx_compra_real():
    """Forma REAL de una primera compra: la curva aparece en los dos lados
    (le salen los tokens que compramos) y nuestra ATA solo en post."""
    meta = FakeMeta(
        pre_balances=[2_000_000_000, 0],
        post_balances=[1_948_800_000, 0],  # -0.0512 SOL (compra + fees + rent de la ATA)
        pre_token_balances=[
            PubkeyTokenBalance(REAL_MINT, CURVE_ACCOUNT_OWNER, 793_100_000.0),
        ],
        post_token_balances=[
            PubkeyTokenBalance(REAL_MINT, CURVE_ACCOUNT_OWNER, 792_112_345.7),
            PubkeyTokenBalance(REAL_MINT, REAL_WALLET, 987_654.3),
        ],
    )
    return FakeConfirmedTx(meta, [Pubkey.from_string(REAL_WALLET), Pubkey.from_string(REAL_MINT)])


def test_fetch_actual_fill_compra_ignora_la_cuenta_de_la_bonding_curve(monkeypatch):
    tx = _tx_compra_real()
    monkeypatch.setattr(txinfo_module, "AsyncClient", make_fake_async_client(tx))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "https://fake-rpc.test", REAL_MINT))

    assert fill is not None
    # Los tokens REALES comprados, no la diferencia contra el saldo de la curva.
    assert fill["token_delta"] == pytest.approx(987_654.3)
    assert fill["sol_delta"] == pytest.approx(-0.0512)


def test_fetch_actual_fill_venta_con_la_cuenta_del_pool_presente(monkeypatch):
    """La otra mitad del mismo caso: en la venta nuestra ATA está en los
    dos lados y la de la curva/pool también -hay que seguir midiendo solo
    la nuestra."""
    meta = FakeMeta(
        pre_balances=[1_948_800_000, 0],
        post_balances=[2_029_800_000, 0],  # +0.081 SOL
        pre_token_balances=[
            PubkeyTokenBalance(REAL_MINT, CURVE_ACCOUNT_OWNER, 792_112_345.7),
            PubkeyTokenBalance(REAL_MINT, REAL_WALLET, 987_654.3),
        ],
        post_token_balances=[
            PubkeyTokenBalance(REAL_MINT, CURVE_ACCOUNT_OWNER, 793_100_000.0),
            PubkeyTokenBalance(REAL_MINT, REAL_WALLET, 0.0),
        ],
    )
    tx = FakeConfirmedTx(meta, [Pubkey.from_string(REAL_WALLET), Pubkey.from_string(REAL_MINT)])
    monkeypatch.setattr(txinfo_module, "AsyncClient", make_fake_async_client(tx))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "https://fake-rpc.test", REAL_MINT))

    assert fill is not None
    assert fill["token_delta"] == pytest.approx(-987_654.3)
    assert fill["sol_delta"] == pytest.approx(0.081)


def test_fetch_actual_fill_none_si_ninguna_cuenta_con_owner_es_nuestra(monkeypatch):
    """Si el nodo SÍ manda `owner` y ninguna de las cuentas del mint es de
    la wallet, no hay nada que atribuirnos -> estimado, no adivinar."""
    meta = FakeMeta(
        pre_balances=[2_000_000_000, 0],
        post_balances=[1_900_000_000, 0],
        pre_token_balances=[PubkeyTokenBalance(REAL_MINT, CURVE_ACCOUNT_OWNER, 793_100_000.0)],
        post_token_balances=[PubkeyTokenBalance(REAL_MINT, CURVE_ACCOUNT_OWNER, 792_000_000.0)],
    )
    tx = FakeConfirmedTx(meta, [Pubkey.from_string(REAL_WALLET), Pubkey.from_string(REAL_MINT)])
    monkeypatch.setattr(txinfo_module, "AsyncClient", make_fake_async_client(tx))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "https://fake-rpc.test", REAL_MINT))

    assert fill is None


def test_fetch_actual_fill_suma_varias_cuentas_propias_del_mismo_mint(monkeypatch):
    """Caso raro pero posible (la wallet ya tenía otra token account de
    ese mint además de la ATA): el movimiento real es la suma de las
    nuestras, no la primera que aparezca."""
    meta = FakeMeta(
        pre_balances=[2_000_000_000, 0],
        post_balances=[1_900_000_000, 0],
        pre_token_balances=[PubkeyTokenBalance(REAL_MINT, REAL_WALLET, 100.0)],
        post_token_balances=[
            PubkeyTokenBalance(REAL_MINT, REAL_WALLET, 100.0),
            PubkeyTokenBalance(REAL_MINT, REAL_WALLET, 400.0),
        ],
    )
    tx = FakeConfirmedTx(meta, [Pubkey.from_string(REAL_WALLET), Pubkey.from_string(REAL_MINT)])
    monkeypatch.setattr(txinfo_module, "AsyncClient", make_fake_async_client(tx))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "https://fake-rpc.test", REAL_MINT))

    assert fill is not None
    assert fill["token_delta"] == pytest.approx(400.0)


def test_fetch_actual_fill_con_wallet_no_supone_que_paga_la_tx(monkeypatch):
    """Orden de Jupiter enrutada por un market maker: quien paga la tx
    (índice 0) no es nuestra wallet. Con `wallet`, el delta de SOL y de
    tokens es el de nuestra cuenta, esté donde esté."""
    meta = FakeMeta(
        pre_balances=[5_000_000_000, 1_000_000_000],
        post_balances=[4_000_000_000, 1_060_000_000],
        pre_token_balances=[FakeTokenBalance(MINT, WALLET, 1000.0)],
        post_token_balances=[FakeTokenBalance(MINT, WALLET, 0.0)],
    )
    resp = FakeGetTransactionResp(FakeConfirmedTx(meta, [OTHER_ACCOUNT, WALLET]))
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    fill = asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT, wallet=WALLET))

    assert fill["sol_delta"] == pytest.approx(0.06)
    assert fill["token_delta"] == pytest.approx(-1000.0)
    assert fill["wallet"] == WALLET


def test_fetch_actual_fill_con_wallet_que_no_esta_en_la_tx(monkeypatch):
    resp = _make_resp(sol_delta_lamports=-1, pre_tokens=0.0, post_tokens=1.0)
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))

    assert asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT,
                                                       wallet="NoEsta1111111111111111111111111111111111111")) is None


# Rent de la cuenta del token: la cuenta 2 es la nuestra (2_074_080 lamports,
# lo que cuesta una de Token-2022 con la extensión de impuesto).
TOKEN_ACCOUNT_LAMPORTS = 2_074_080


def _resp_con_cuenta(pre_tokens, post_tokens, sol_delta_lamports):
    pre = [FakeTokenBalance(MINT, WALLET, pre_tokens, 2)] if pre_tokens is not None else []
    post = [FakeTokenBalance(MINT, WALLET, post_tokens, 2)] if post_tokens is not None else []
    meta = FakeMeta(
        pre_balances=[1_000_000_000, 5_000_000_000, 0 if pre_tokens is None else TOKEN_ACCOUNT_LAMPORTS],
        post_balances=[1_000_000_000 + sol_delta_lamports, 5_000_000_000,
                       0 if post_tokens is None else TOKEN_ACCOUNT_LAMPORTS],
        pre_token_balances=pre, post_token_balances=post,
    )
    return FakeGetTransactionResp(FakeConfirmedTx(meta, [WALLET, OTHER_ACCOUNT, "TokenAcc"]))


def _fill(monkeypatch, resp):
    monkeypatch.setattr(txinfo_module, "AsyncClient", lambda url: FakeAsyncClient(resp=resp))
    return asyncio.run(txinfo_module.fetch_actual_fill(FAKE_SIGNATURE, "http://fake-rpc", MINT))


def test_compra_que_crea_la_cuenta_del_token_reporta_el_rent_pagado(monkeypatch):
    fill = _fill(monkeypatch, _resp_con_cuenta(None, 1000.0, -52_000_000))
    assert fill["rent_paid_sol"] == pytest.approx(0.00207408)
    assert fill["rent_refund_sol"] == 0.0


def test_venta_que_cierra_la_cuenta_del_token_reporta_el_rent_devuelto(monkeypatch):
    fill = _fill(monkeypatch, _resp_con_cuenta(1000.0, None, 60_000_000))
    assert fill["rent_paid_sol"] == 0.0
    assert fill["rent_refund_sol"] == pytest.approx(0.00207408)


def test_venta_que_deja_la_cuenta_abierta_no_devuelve_rent(monkeypatch):
    fill = _fill(monkeypatch, _resp_con_cuenta(1000.0, 0.0, 58_000_000))
    assert fill["rent_refund_sol"] == 0.0


def test_sin_account_index_no_se_descuenta_rent(monkeypatch):
    """Sin poder identificar la cuenta, el rent se trata como coste (como antes)."""
    resp = _make_resp(sol_delta_lamports=-50_500_000, pre_tokens=None, post_tokens=1_234.5)
    fill = _fill(monkeypatch, resp)
    assert fill["rent_paid_sol"] == 0.0 and fill["rent_refund_sol"] == 0.0
