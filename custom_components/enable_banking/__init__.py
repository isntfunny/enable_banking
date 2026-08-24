"""The Enable Banking integration (Open Banking / PSD2 account data)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import Platform
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .client import (
    AccountDetails,
    EnableBankingAuthError,
    EnableBankingClient,
    EnableBankingError,
    EnableBankingSessionExpiredError,
)
from .const import (
    CONF_ACCOUNTS,
    CONF_APPLICATION_ID,
    CONF_PRIVATE_KEY,
    CONF_SESSION_ID,
    CONF_VALID_UNTIL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ISSUE_CONSENT_EXPIRING,
    RENEWAL_WARNING_DAYS,
)
from .coordinator import EnableBankingDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.EVENT]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration (no YAML support — config-entry only)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Enable Banking from a config entry.

    Verifies the AIS session is still authorised, fetches account details
    once (used for device/entity naming), then creates a single
    DataUpdateCoordinator that polls balances + transactions for every
    linked account per cycle.
    """
    data = entry.data
    client = EnableBankingClient(hass, data[CONF_APPLICATION_ID], data[CONF_PRIVATE_KEY])
    account_uids: list[str] = data[CONF_ACCOUNTS]
    session_id: str = data[CONF_SESSION_ID]

    try:
        session = await client.get_session(session_id)
    except EnableBankingSessionExpiredError as err:
        raise ConfigEntryAuthFailed("Enable Banking consent expired") from err
    except EnableBankingAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except EnableBankingError as err:
        raise ConfigEntryNotReady(f"Enable Banking API unreachable: {err}") from err

    if session.status != "AUTHORIZED":
        raise ConfigEntryAuthFailed(
            f"Session status is '{session.status}', expected AUTHORIZED"
        )

    # The consent expiry is only known from the API, so refresh the stored
    # copy on every setup — it is what the expiry sensor and the renewal
    # warning below are based on.
    if session.valid_until and session.valid_until != data.get(CONF_VALID_UNTIL):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_VALID_UNTIL: session.valid_until}
        )

    _async_check_consent_expiry(hass, entry, session.valid_until)

    account_details: dict[str, AccountDetails] = {}
    for uid in account_uids:
        try:
            account_details[uid] = await client.get_account_details(uid)
        except EnableBankingError as err:  # noqa: PERF203
            _LOGGER.warning("Could not fetch details for account %s: %s", uid, err)

    coordinator = EnableBankingDataUpdateCoordinator(
        hass, client, account_uids, DEFAULT_UPDATE_INTERVAL
    )
    coordinator.config_entry = entry

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
        "account_uids": account_uids,
        "account_details": account_details,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _async_check_consent_expiry(
    hass: HomeAssistant, entry: ConfigEntry, valid_until: str | None
) -> None:
    """Raise a repairs issue while there is still time to act.

    PSD2 mandates a fresh SCA to renew, so this cannot be automated — the
    point of the lead time is that the user can pick a moment to go through
    the bank login before the data silently goes stale.
    """
    issue_id = f"{ISSUE_CONSENT_EXPIRING}_{entry.entry_id}"

    if not valid_until:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    expires = dt_util.parse_datetime(valid_until)
    if expires is None:
        _LOGGER.debug("Unparseable consent expiry %r", valid_until)
        return

    days_left = (expires - dt_util.utcnow()).days
    if days_left > RENEWAL_WARNING_DAYS:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="consent_expiring",
        translation_placeholders={
            "title": entry.title,
            "days": str(max(days_left, 0)),
        },
        learn_more_url="https://enablebanking.com/docs/",
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entries.

    Version 1 stored a user-supplied ``redirect_url``; the redirect target is
    now derived from Home Assistant's own external URL, so the field is
    dropped. ``valid_until`` is left empty here on purpose — the next setup
    fills it in from the API. No re-authorisation needed.
    """
    if entry.version >= 2:
        return True

    data = {k: v for k, v in entry.data.items() if k != "redirect_url"}
    data.setdefault(CONF_VALID_UNTIL, None)
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    _LOGGER.debug("Migrated %s from version 1 to 2", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
