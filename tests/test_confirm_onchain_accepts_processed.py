"""
Test de regresión para PumpPortalClient._confirm_transaction_onchain:
a propósito se conforma con CUALQUIER confirmation_status no-nulo,
incluido "processed" (el nivel más laxo, casi instantáneo). Es una
decisión consciente para no retrasar la apertura de la posición -ver
pump.py:_fetch_actual_fill, que es quien absorbe el desfasaje que esto
puede causar contra getTransaction, con sus propios reintentos y sin
bloquear la compra.
"""
import asyncio

import pytest
from solders.signature import Signature
from solders.transaction_status import TransactionConfirmationStatus

from pepump import pump as pump_module
from pepump.pump import PumpPortalClient

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
    monkeypatch.setattr(pump_module, "AsyncClient", lambda url: fake_client)

    asyncio.run(PumpPortalClient._confirm_transaction_onchain(
        FAKE_SIGNATURE, "http://fake-rpc", timeout_seconds=5.0, poll_interval_seconds=0
    ))

    assert fake_client.calls == 1  # no debe esperar a confirmed/finalized
