"""Home Assistant fixtures with bank traffic intercepted at the HTTP boundary."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enable_banking.const import API_BASE, DOMAIN


@pytest.fixture(autouse=True)
def custom_integration(enable_custom_integrations):
    """Allow loading the integration from this repository."""


@pytest.fixture(autouse=True)
def client_auth():
    """No real application key or bank credentials are needed."""
    with patch(
        "custom_components.enable_banking.client.EnableBankingClient._auth_header",
        new=AsyncMock(return_value={"Authorization": "Bearer test-token"}),
    ):
        yield


@pytest.fixture
def entry():
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Bank",
        version=2,
        data={
            "application_id": "test-app",
            "private_key": "test-key",
            "aspsp_name": "Test Bank",
            "aspsp_country": "DE",
            "session_id": "test-session",
            "accounts": ["account-a", "account-b"],
        },
        options={"psu_ip": "203.0.113.10"},
    )


@pytest.fixture
def bank_api(aioclient_mock):
    aioclient_mock.get(
        f"{API_BASE}/sessions/test-session",
        json={"status": "AUTHORIZED", "accounts": ["account-a", "account-b"]},
    )
    for uid in ("account-a", "account-b"):
        aioclient_mock.get(
            f"{API_BASE}/accounts/{uid}/details",
            json={"uid": uid, "product": uid, "currency": "EUR"},
        )
        aioclient_mock.get(
            f"{API_BASE}/accounts/{uid}/balances",
            json={
                "balances": [
                    {
                        "balance_amount": {"amount": "12.34", "currency": "EUR"},
                        "balance_type": "CLBD",
                    }
                ]
            },
        )
        aioclient_mock.get(
            f"{API_BASE}/accounts/{uid}/transactions",
            json={
                "transactions": [
                    {
                        "entry_reference": f"{uid}-transaction",
                        "status": "PDNG",
                        "credit_debit_indicator": "DBIT",
                        "transaction_amount": {"amount": "2.50", "currency": "EUR"},
                    }
                ]
            },
        )
    return aioclient_mock
