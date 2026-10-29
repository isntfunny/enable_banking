"""PSU IP configuration is available during the initial bank connection."""

from custom_components.enable_banking.const import API_BASE, DOMAIN


def user_input(psu_ip):
    return {
        "application_id": "test-app",
        "private_key": "test-key",
        "aspsp_name": "Test Bank",
        "aspsp_country": "DE",
        "psu_type": "personal",
        "valid_days": 180,
        "psu_ip": psu_ip,
    }


async def test_initial_config_stores_ip_and_uses_it_for_startup(hass, bank_api):
    hass.config.external_url = "https://ha.example.com"
    bank_api.get(f"{API_BASE}/application", json={"active": True})
    bank_api.post(f"{API_BASE}/auth", json={"url": "https://bank.example.com/auth"})
    bank_api.post(
        f"{API_BASE}/sessions",
        json={
            "session_id": "test-session",
            "accounts": [{"uid": "account-a"}, {"uid": "account-b"}],
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data=user_input(" 198.51.100.7 ")
    )
    assert result["type"] == "external"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "test-code"}
    )
    assert result["type"] == "external_done"
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] == "create_entry"
    assert result["data"]["psu_ip"] == "198.51.100.7"
    await hass.async_block_till_done()
    account_calls = [c for c in bank_api.mock_calls if "/accounts/" in c[1].path]
    assert len(account_calls) == 6
    assert all(c[3].get("Psu-Ip-Address") == "198.51.100.7" for c in account_calls)


async def test_initial_config_rejects_invalid_ip_without_bank_request(hass, bank_api):
    hass.config.external_url = "https://ha.example.com"
    bank_api.clear_requests()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data=user_input("not-an-ip")
    )
    assert result["type"] == "form"
    assert result["errors"] == {"psu_ip": "invalid_ip"}
    assert result["description_placeholders"]["applications_url"] == (
        "https://enablebanking.com/cp/applications"
    )
    assert bank_api.call_count == 0
