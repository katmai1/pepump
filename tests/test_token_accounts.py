"""
Tests del cierre de cuentas de token vacías (token_accounts.py) y de su uso
tras una venta real (TradeExecutor.sell).

Caso real: venta de 6vwkHv...pump por PumpPortal; la cuenta del token quedó
abierta con saldo 0 y 0.00151384 SOL de rent, y el historial registró
-0.55% en una operación que, con el rent recuperado, era de +6%.
"""
import asyncio
from types import SimpleNamespace

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction
from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID

from pepump import token_accounts as module
from pepump.executor import Position, TradeExecutor
from pepump.token_accounts import TokenAccountCloser, build_token_account_closer
from tests.conftest import make_config

MINT = "6vwkHvvoJujaPy6SWdjqE2fFc1V3AW6pj6KTh9jipump"


def _acc(mint=MINT, amount="0", lamports=1_513_840, program=TOKEN_PROGRAM_ID, state="initialized",
         extensions=None):
    info = {"mint": mint, "tokenAmount": {"amount": amount}, "state": state}
    if extensions is not None:
        info["extensions"] = extensions
    return SimpleNamespace(pubkey=Pubkey.new_unique(),
                           account=SimpleNamespace(owner=program, lamports=lamports,
                                                   data=SimpleNamespace(parsed={"info": info})))


class FakeRpc:
    """Doble de AsyncClient: cuentas por programa, y registro de las tx
    enviadas. `reject` = cuentas cuyo cierre falla en la simulación."""

    def __init__(self, accounts, reject=()):
        self.accounts = accounts
        self.reject = set(reject)
        self.sent: list = []
        self.filters: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_token_accounts_by_owner_json_parsed(self, owner, opts, commitment=None):
        self.filters.append(opts)
        if opts.mint is not None:
            value = [a for a in self.accounts if a.account.data.parsed["info"]["mint"] == str(opts.mint)]
        else:
            value = [a for a in self.accounts if a.account.owner == opts.program_id]
        return SimpleNamespace(value=value)

    async def get_latest_blockhash(self):
        return SimpleNamespace(value=SimpleNamespace(blockhash=Hash.default()))

    async def send_raw_transaction(self, raw, opts=None):
        tx = Transaction.from_bytes(raw)
        cerradas = {tx.message.account_keys[ix.accounts[0]] for ix in tx.message.instructions}
        if cerradas & self.reject:
            raise RuntimeError("simulación fallida (fake)")
        self.sent.append(cerradas)
        return SimpleNamespace(value=Signature.default())


@pytest.fixture
def rpc(monkeypatch):
    holder = {}

    def install(accounts, reject=()):
        holder["rpc"] = FakeRpc(accounts, reject)
        monkeypatch.setattr(module, "AsyncClient", lambda url: holder["rpc"])

        async def confirmed(*args):
            return None
        monkeypatch.setattr(module.PumpPortalClient, "_confirm_transaction_onchain", staticmethod(confirmed))
        return holder["rpc"]
    return install


def _closer():
    return TokenAccountCloser(Keypair(), "http://rpc")


def test_solo_cierra_cuentas_vacias_y_cerrables(rpc):
    vacia = _acc()
    vacia_2022 = _acc(mint="Otro111111111111111111111111111111111111111", program=TOKEN_2022_PROGRAM_ID,
                      lamports=2_074_080)
    fake = rpc([vacia, vacia_2022, _acc(amount="5"), _acc(state="frozen"),
                _acc(program=TOKEN_2022_PROGRAM_ID,
                     extensions=[{"extension": "transferFeeAmount", "state": {"withheldAmount": 7}}])])

    recuperado, cerradas, fallidas = asyncio.run(_closer().close_empty())

    assert cerradas == 2 and fallidas == 0
    assert fake.sent == [{vacia.pubkey, vacia_2022.pubkey}]  # un único lote
    assert recuperado == pytest.approx((1_513_840 + 2_074_080 - 5_000) / 1e9)


def test_con_mint_solo_mira_ese_mint(rpc):
    fake = rpc([_acc(), _acc(mint="Otro111111111111111111111111111111111111111")])

    _, cerradas, _ = asyncio.run(_closer().close_empty(MINT))

    assert cerradas == 1
    assert [str(f.mint) for f in fake.filters] == [MINT]


def test_si_una_no_se_deja_cerrar_cierra_las_demas_de_una_en_una(rpc):
    buena, mala = _acc(), _acc()
    fake = rpc([buena, mala], reject=[mala.pubkey])

    recuperado, cerradas, fallidas = asyncio.run(_closer().close_empty())

    assert cerradas == 1 and fallidas == 1
    assert fake.sent == [{buena.pubkey}]
    assert recuperado == pytest.approx((1_513_840 - 5_000) / 1e9)


def test_lotes_de_como_mucho_veinte(rpc):
    fake = rpc([_acc() for _ in range(25)])

    _, cerradas, _ = asyncio.run(_closer().close_empty())

    assert cerradas == 25
    assert [len(lote) for lote in fake.sent] == [20, 5]


def test_build_closer_necesita_clave_y_la_opcion():
    kp = Keypair()
    assert build_token_account_closer(make_config()) is None
    assert build_token_account_closer(make_config(wallet_private_key=str(kp),
                                                  close_empty_token_accounts=False)) is None
    assert build_token_account_closer(make_config(wallet_private_key=str(kp))) is not None
    with pytest.raises(ValueError, match="Tienen que ser la misma"):
        build_token_account_closer(make_config(wallet_private_key=str(kp),
                                               wallet_address=str(Keypair().pubkey())))


# --------------------------------------------------------------------------- #
# Tras una venta real
# --------------------------------------------------------------------------- #

class FakeLightning:
    def __init__(self, result):
        self.result = result

    async def execute_lightning_trade(self, **kwargs):
        return self.result


class FakeCloser:
    """`results`: lo que devuelve cada llamada a close_after_sale,
    (recuperado, cerradas, fallidas, pendiente); se queda en el último.
    `fail`: todas lanzan."""

    def __init__(self, recovered=0.00150884, fail=False, results=None):
        self.results = results or [(recovered, 1, 0, False)]
        self.fail = fail
        self.calls = []

    async def close_after_sale(self, mint, route_mints=()):
        self.calls.append((mint, set(route_mints)))
        if self.fail:
            raise RuntimeError("RPC caído (fake)")
        return self.results[min(len(self.calls), len(self.results)) - 1]


def _sell(result, closer, tmp_path):
    cfg = make_config(live=True, trade_history_csv=str(tmp_path / "h.csv"))
    executor = TradeExecutor(client=FakeLightning(result), live=True, config=cfg, closer=closer)
    executor.CLOSE_ACCOUNT_RETRY_SECONDS = 0.0
    pos = Position(mint=MINT, entry_price=1.4362841631e-06, sol_amount=0.023718053, token_amount=15459.484669)
    asyncio.run(executor.sell(pos, 1.53e-06, "trailing"))
    return pos


def test_el_caso_real_cierra_la_cuenta_y_el_historial_sale_en_positivo(tmp_path):
    closer = FakeCloser()
    pos = _sell({"signature": "S", "actual_sol_delta": 0.0235873, "actual_token_delta": -15459.484669},
                closer, tmp_path)

    assert closer.calls == [(MINT, set())]
    assert pos.proceeds_sol == pytest.approx(0.0235873 + 0.00150884)
    assert pos.realized_pnl_sol > 0
    # El precio de salida no cuenta el rent.
    assert pos.exit_price == pytest.approx(0.0235873 / 15459.484669)


def test_si_la_venta_ya_cerro_la_cuenta_no_hay_nada_mas_que_recuperar(tmp_path):
    closer = FakeCloser(results=[(0.0, 0, 0, False)])
    pos = _sell({"signature": "S", "actual_sol_delta": 0.025, "actual_token_delta": -15459.484669,
                 "actual_rent_refund_sol": 0.0015}, closer, tmp_path)
    assert len(closer.calls) == 1  # sin reintentos: no queda nada pendiente
    assert pos.proceeds_sol == pytest.approx(0.025)


def test_cierra_tambien_las_cuentas_intermedias_de_la_ruta(tmp_path):
    """Caso real: SOL -> USDC -> TTWO -> Macca por Jupiter dejó abiertas
    las cuentas de USDC y TTWO (0.003 SOL de rent)."""
    usdc, ttwo, otro = "USDC" + "1" * 40, "TTWO" + "1" * 40, "Otro" + "1" * 40
    closer = FakeCloser(results=[(0.00454, 3, 0, False)])
    cfg = make_config(live=True, trade_history_csv=str(tmp_path / "h.csv"))
    executor = TradeExecutor(client=FakeLightning({"signature": "S", "actual_sol_delta": 0.034,
                                                   "actual_token_delta": -1000.0,
                                                   "actual_created_mints": [otro]}),
                             live=True, config=cfg, closer=closer)
    pos = Position(mint=MINT, entry_price=2e-6, sol_amount=0.0345, token_amount=1000.0,
                   route_mints=[usdc, ttwo])

    asyncio.run(executor.sell(pos, 2e-6, "trailing"))

    assert closer.calls == [(MINT, {usdc, ttwo, otro})]
    assert pos.proceeds_sol == pytest.approx(0.034 + 0.00454)


def test_si_el_cierre_falla_la_venta_se_registra_igual(tmp_path):
    pos = _sell({"signature": "S", "actual_sol_delta": 0.0235873, "actual_token_delta": -15459.484669},
                FakeCloser(fail=True), tmp_path)

    assert pos.closed
    assert pos.proceeds_sol == pytest.approx(0.0235873)


def test_en_simulado_no_se_toca_la_wallet(tmp_path):
    closer = FakeCloser()
    cfg = make_config(trade_history_csv=str(tmp_path / "h.csv"))
    executor = TradeExecutor(client=None, live=False, config=cfg, closer=closer)
    pos = Position(mint=MINT, entry_price=1e-6, sol_amount=0.02, token_amount=20000.0)

    asyncio.run(executor.sell(pos, 1e-6, "trailing"))

    assert closer.calls == []


def test_si_el_nodo_aun_no_ve_la_venta_reintenta_el_cierre(tmp_path, caplog):
    """Caso real: justo tras vender, el cierre falló con NonNativeHasBalance
    (0xb) porque el nodo aún veía los tokens. Se reintenta."""
    closer = FakeCloser(results=[(0.0, 0, 1, False), (0.0, 0, 0, True), (0.00150884, 1, 0, False)])
    pos = _sell({"signature": "S", "actual_sol_delta": 0.020364574, "actual_token_delta": -1108.253805},
                closer, tmp_path)

    assert len(closer.calls) == 3
    assert pos.proceeds_sol == pytest.approx(0.020364574 + 0.00150884)
    assert "No se pudo cerrar" not in caplog.text


def test_si_no_se_puede_cerrar_avisa_una_sola_vez(tmp_path, caplog):
    closer = FakeCloser(results=[(0.0, 0, 1, False)])
    _sell({"signature": "S", "actual_sol_delta": 0.02, "actual_token_delta": -1000.0}, closer, tmp_path)

    assert len(closer.calls) == 3
    assert caplog.text.count("No se pudieron cerrar todas las cuentas vacías") == 1


def test_la_simulacion_va_contra_confirmed(rpc):
    """solana-py simula contra "finalized" por defecto: justo tras vender,
    ahí la cuenta todavía tiene los tokens."""
    from solana.rpc.commitment import Confirmed
    fake = rpc([_acc()])
    opts_vistos = []
    original = fake.send_raw_transaction

    async def espia(raw, opts=None):
        opts_vistos.append(opts)
        return await original(raw, opts)
    fake.send_raw_transaction = espia

    asyncio.run(_closer().close_empty())

    assert opts_vistos[0].preflight_commitment == Confirmed and not opts_vistos[0].skip_preflight



def test_close_after_sale_cierra_el_token_y_los_intermedios_vacios(rpc):
    usdc = str(Pubkey.new_unique())
    usdc_vacia = _acc(mint=usdc)
    usdc_con_saldo = _acc(mint=usdc, amount="5000000")  # USDC propios: no se tocan
    token = _acc()
    fake = rpc([token, usdc_vacia, usdc_con_saldo])

    recuperado, cerradas, fallidas, pendiente = asyncio.run(
        _closer().close_after_sale(MINT, [usdc, MINT]))

    assert (cerradas, fallidas, pendiente) == (2, 0, False)
    assert fake.sent == [{token.pubkey, usdc_vacia.pubkey}]


def test_close_after_sale_pendiente_si_el_token_aun_tiene_saldo(rpc):
    rpc([_acc(amount="1000")])
    _, cerradas, _, pendiente = asyncio.run(_closer().close_after_sale(MINT))
    assert cerradas == 0 and pendiente
