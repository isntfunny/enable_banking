"""Refresh requests must carry the IP only for startup and manual actions."""

import asyncio
from datetime import timedelta
import importlib.util
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.enable_banking.const import API_BASE, DOMAIN


async def setup_entry(hass, entry):
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


def button_id(hass, entry):
    return er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_refresh_balances"
    )


async def press_button(hass, entry):
    entity_id = button_id(hass, entry)
    assert entity_id is not None
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )


def mock_balances(bank_api, amount="99.00", *, status=200):
    bank_api.clear_requests()
    for uid in ("account-a", "account-b"):
        bank_api.get(
            f"{API_BASE}/accounts/{uid}/balances",
            status=status,
            json={
                "balances": [
                    {
                        "balance_amount": {"amount": amount, "currency": "EUR"},
                        "balance_type": "CLBD",
                    }
                ]
            },
        )
        bank_api.get(
            f"{API_BASE}/accounts/{uid}/transactions", json={"transactions": []}
        )


async def test_startup_sends_ip_on_all_account_endpoints(hass, entry, bank_api):
    await setup_entry(hass, entry)
    account_calls = [c for c in bank_api.mock_calls if "/accounts/" in c[1].path]
    assert len(account_calls) == 6
    assert all(c[3].get("Psu-Ip-Address") == "203.0.113.10" for c in account_calls)


async def test_background_poll_does_not_reuse_startup_ip(hass, entry, bank_api):
    coordinator = await setup_entry(hass, entry)
    mock_balances(bank_api)
    await coordinator.async_refresh()
    assert (
        coordinator.data.accounts["account-a"].balances[0].balance_amount.amount
        == "99.00"
    )
    assert all("Psu-Ip-Address" not in c[3] for c in bank_api.mock_calls)


async def test_button_refreshes_all_balances_and_preserves_transactions(
    hass, entry, bank_api
):
    coordinator = await setup_entry(hass, entry)
    old_transactions = coordinator.data.accounts["account-a"].transactions
    mock_balances(bank_api)
    await press_button(hass, entry)
    assert (
        coordinator.data.accounts["account-a"].balances[0].balance_amount.amount
        == "99.00"
    )
    assert (
        coordinator.data.accounts["account-b"].balances[0].balance_amount.amount
        == "99.00"
    )
    assert coordinator.data.accounts["account-a"].transactions == old_transactions
    assert not coordinator.data.new_transactions
    assert len(bank_api.mock_calls) == 2
    assert all(c[1].path.endswith("/balances") for c in bank_api.mock_calls)
    assert all(
        c[3].get("Psu-Ip-Address") == "203.0.113.10" for c in bank_api.mock_calls
    )


async def test_button_remains_available_after_background_rate_limit(
    hass, entry, bank_api
):
    coordinator = await setup_entry(hass, entry)
    mock_balances(bank_api, status=429)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert hass.states.get(button_id(hass, entry)).state != "unavailable"
    mock_balances(bank_api)
    await press_button(hass, entry)
    assert coordinator.last_update_success
    assert (
        coordinator.data.accounts["account-a"].balances[0].balance_amount.amount
        == "99.00"
    )


async def test_failed_manual_refresh_preserves_previous_data(hass, entry, bank_api):
    coordinator = await setup_entry(hass, entry)
    old_data = coordinator.data
    bank_api.clear_requests()
    bank_api.get(f"{API_BASE}/accounts/account-a/balances", json={"balances": []})
    bank_api.get(
        f"{API_BASE}/accounts/account-b/balances",
        status=429,
        json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"},
    )
    with pytest.raises(HomeAssistantError):
        await press_button(hass, entry)
    assert coordinator.data is old_data


async def test_existing_entry_without_ip_still_loads(hass, entry, bank_api):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={})
    await setup_entry(hass, entry)
    assert all("Psu-Ip-Address" not in c[3] for c in bank_api.mock_calls)
    bank_api.clear_requests()
    with pytest.raises(HomeAssistantError, match="IP"):
        await press_button(hass, entry)
    assert bank_api.call_count == 0


@pytest.mark.parametrize("psu_ip", ["198.51.100.7", "2001:db8::1", ""])
async def test_existing_entry_can_configure_ip(hass, entry, psu_ip):
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"psu_ip": psu_ip}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options["psu_ip"] == psu_ip


async def test_options_reject_invalid_ip(hass, entry):
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"psu_ip": "https://example.com"}
    )
    assert result["type"] == "form"
    assert result["errors"] == {"psu_ip": "invalid_ip"}


async def test_manual_refresh_does_not_leak_headers_into_concurrent_poll(
    hass, entry, bank_api
):
    coordinator = await setup_entry(hass, entry)
    mock_balances(bank_api)
    bank_api.clear_requests()
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release(method, url, data):
        started.set()
        await release.wait()
        return AiohttpClientMockResponse(method, url, json={"balances": []})

    bank_api.get(
        f"{API_BASE}/accounts/account-a/balances", side_effect=wait_for_release
    )
    bank_api.get(f"{API_BASE}/accounts/account-b/balances", json={"balances": []})
    for uid in ("account-a", "account-b"):
        bank_api.get(
            f"{API_BASE}/accounts/{uid}/transactions", json={"transactions": []}
        )

    manual = asyncio.create_task(press_button(hass, entry))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        background = asyncio.create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        assert bank_api.call_count == 1
    finally:
        release.set()
        await manual
    await background
    assert len(bank_api.mock_calls) == 6
    assert all(
        c[3].get("Psu-Ip-Address") == "203.0.113.10" for c in bank_api.mock_calls[:2]
    )
    assert all("Psu-Ip-Address" not in c[3] for c in bank_api.mock_calls[2:])


async def test_scheduled_poll_waits_six_hours(hass, entry, bank_api, freezer):
    await setup_entry(hass, entry)
    mock_balances(bank_api)
    start = dt_util.utcnow()
    freezer.move_to(start + timedelta(hours=4, seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert bank_api.call_count == 0
    freezer.move_to(start + timedelta(hours=6, seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert bank_api.call_count == 4
    assert all("Psu-Ip-Address" not in c[3] for c in bank_api.mock_calls)


async def test_startup_uses_ip_from_new_entry_data(hass, entry, bank_api):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={}, data={**entry.data, "psu_ip": "198.51.100.7"}
    )
    await setup_entry(hass, entry)
    account_calls = [c for c in bank_api.mock_calls if "/accounts/" in c[1].path]
    assert all(c[3].get("Psu-Ip-Address") == "198.51.100.7" for c in account_calls)


async def test_clearing_option_overrides_ip_in_entry_data(hass, entry, bank_api):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={"psu_ip": ""}, data={**entry.data, "psu_ip": "198.51.100.7"}
    )
    await setup_entry(hass, entry)
    assert all("Psu-Ip-Address" not in c[3] for c in bank_api.mock_calls)


async def test_manual_auth_failure_starts_reauthentication(hass, entry, bank_api):
    coordinator = await setup_entry(hass, entry)
    previous = coordinator.data
    bank_api.clear_requests()
    bank_api.get(f"{API_BASE}/accounts/account-a/balances", status=403, json={})
    with patch.object(type(entry), "async_start_reauth") as reauth:
        with pytest.raises(HomeAssistantError):
            await press_button(hass, entry)
        reauth.assert_called_once_with(hass)
    assert coordinator.data is previous


async def test_german_button_name_includes_bank(hass, entry, bank_api):
    hass.config.language = "de"
    await setup_entry(hass, entry)
    button = hass.states.get(button_id(hass, entry))
    assert button.attributes["friendly_name"] == "Test Bank Kontostände aktualisieren"


async def test_manual_balance_refresh_keeps_transaction_poll_deadline(
    hass, entry, bank_api, freezer
):
    await setup_entry(hass, entry)
    mock_balances(bank_api)
    start = dt_util.utcnow()
    freezer.move_to(start + timedelta(hours=5))
    await press_button(hass, entry)
    bank_api.mock_calls.clear()
    freezer.move_to(start + timedelta(hours=6, seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert len(bank_api.mock_calls) == 4
    assert sum(c[1].path.endswith("/transactions") for c in bank_api.mock_calls) == 2


async def test_options_support_older_home_assistant(hass, entry):
    """Simulate the options API available at our advertised minimum version."""
    from custom_components.enable_banking import config_flow

    entry.add_to_hass(hass)
    spec = importlib.util.spec_from_file_location(
        "custom_components.enable_banking.legacy_config_flow", config_flow.__file__
    )
    legacy = importlib.util.module_from_spec(spec)
    with patch.dict(config_entries.HANDLERS):
        with patch.object(config_entries, "OptionsFlowWithReload"):
            del config_entries.OptionsFlowWithReload
            spec.loader.exec_module(legacy)
            flow = legacy.EnableBankingConfigFlow.async_get_options_flow(entry)
            flow.hass = hass
            with patch.object(
                hass.config_entries, "async_reload", return_value=True
            ) as reload:
                result = await flow.async_step_init({"psu_ip": "198.51.100.7"})
                await hass.async_block_till_done()
                assert result["type"] == "create_entry"
                assert entry.options["psu_ip"] == "198.51.100.7"
                reload.assert_called_once_with(entry.entry_id)
