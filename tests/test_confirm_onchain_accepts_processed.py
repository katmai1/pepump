"""
Test de regresión para PumpPortalClient._confirm_transaction_onchain:
a propósito se conforma con CUALQUIER confirmation_status no-nulo,
incluido "processed" (el nivel más laxo, casi instantáneo). Es una
decisión consciente para no retrasar la apertura de la posición -ver
onchain/txinfo.py:fetch_actual_fill, que es quien absorbe el desfase que esto
puede causar contra getTransaction, con sus propios reintentos y sin
bloquear la compra.
"""
import asyncio

import pytest
from solders.signature import Signature
from solders.transaction_status import TransactionConfirmationStatus

from pepump import pumpportal as pumpportal_module
from pepump.pumpportal import PumpPortalClient, TradeOutcomeUnknown

FAKE_SIGNATURE = str(Signature.default())


class FakeStatusInfo:
    def __init__(self, confirmation_status, err=None):
        self.confirmation_status = confirmation_status
        self.err = err


class FakeStatusesResp:
    def __init__(self, value):
        self.value = value


class FakeAsyncClient:
    def __init__(self, info):
        self._info = info
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get_signature_statuses(self, sigs, search_transaction_history=True):
        self.calls += 1
        return FakeStatusesResp([self._info])


def test_processed_alcanza_para_confirmar_de_una(monkeypatch):
    fake_client = FakeAsyncClient(FakeStatusInfo(TransactionConfirmationStatus.Processed))
    monkeypatch.setattr(pumpportal_module, "AsyncClient", lambda url: fake_client)

    asyncio.run(PumpPortalClient._confirm_transaction_onchain(
        FAKE_SIGNATURE, "http://fake-rpc", timeout_seconds=5.0, poll_interval_seconds=0
    ))

    assert fake_client.calls == 1  # no debe esperar a confirmed/finalized


class FlakyAsyncClient(FakeAsyncClient):
    """El RPC falla las primeras `failures` consultas (timeout, 429...)."""

    def __init__(self, info, failures):
        super().__init__(info)
        self.failures = failures

    async def get_signature_statuses(self, sigs, search_transaction_history=True):
        if self.failures > 0:
            self.failures -= 1
            self.calls += 1
            raise RuntimeError("ReadTimeout del RPC")
        return await super().get_signature_statuses(sigs, search_transaction_history)


def test_un_fallo_puntual_del_rpc_no_corta_la_confirmacion(monkeypatch):
    """Antes la excepción del RPC subía tal cual y el executor la tomaba
    como orden rechazada, aunque la tx ya estuviera enviada (y entrara)."""
    fake_client = FlakyAsyncClient(FakeStatusInfo(TransactionConfirmationStatus.Confirmed), failures=2)
    monkeypatch.setattr(pumpportal_module, "AsyncClient", lambda url: fake_client)

    asyncio.run(PumpPortalClient._confirm_transaction_onchain(
        FAKE_SIGNATURE, "http://fake-rpc", timeout_seconds=5.0, poll_interval_seconds=0
    ))

    assert fake_client.calls == 3


def test_rpc_caido_hasta_el_plazo_es_resultado_desconocido(monkeypatch):
    fake_client = FlakyAsyncClient(None, failures=10_000)
    monkeypatch.setattr(pumpportal_module, "AsyncClient", lambda url: fake_client)

    with pytest.raises(TradeOutcomeUnknown, match="el RPC falló"):
        asyncio.run(PumpPortalClient._confirm_transaction_onchain(
            FAKE_SIGNATURE, "http://fake-rpc", timeout_seconds=0.05, poll_interval_seconds=0.01
        ))


def test_lightning_200_ilegible_es_resultado_desconocido(monkeypatch):
    class Ilegible:
        status_code = 200
        text = "<html>"

        def json(self):
            raise ValueError("Expecting value")

    monkeypatch.setattr(pumpportal_module.requests, "post", lambda *a, **k: Ilegible())
    with pytest.raises(TradeOutcomeUnknown):
        asyncio.run(PumpPortalClient(api_key="k").execute_lightning_trade(
            action="buy", mint="M", amount=0.05, denominated_in_sol=True, slippage=15,
            priority_fee=0.0, pool="auto", solana_rpc_url="http://fake-rpc"))
