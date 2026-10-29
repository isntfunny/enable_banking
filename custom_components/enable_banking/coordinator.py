"""DataUpdateCoordinator for the Enable Banking integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
import logging

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    AccountBalance,
    AccountDetails,
    EnableBankingAuthError,
    EnableBankingClient,
    EnableBankingError,
    EnableBankingRateLimitError,
    EnableBankingSessionExpiredError,
    Transaction,
)
from .const import DOMAIN, TRANSACTION_LOOKBACK_DAYS

_LOGGER = logging.getLogger(__name__)


def get_account_device_info(
    entry: ConfigEntry, account_uid: str, details: AccountDetails | None
) -> DeviceInfo:
    """One HA device per linked account."""
    name = (details.product if details else None) or account_uid
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{account_uid}")},
        name=name,
        manufacturer="Enable Banking",
        model=details.details if details else None,
    )


@dataclass
class EnableBankingAccountData:
    """Data for a single linked account, refreshed each poll cycle."""

    balances: list[AccountBalance] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)


@dataclass
class EnableBankingCoordinatorData:
    """Aggregated data from a single polling cycle, across all accounts."""

    accounts: dict[str, EnableBankingAccountData] = field(default_factory=dict)
    new_transactions: dict[str, list[Transaction]] = field(default_factory=dict)


class EnableBankingDataUpdateCoordinator(
    DataUpdateCoordinator[EnableBankingCoordinatorData]
):
    """Polls balances + transactions for every linked account.

    Uses the configured PSU IP for the initial refresh and explicit manual
    balance refreshes only. Scheduled background polls omit PSU headers.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: EnableBankingClient,
        account_uids: list[str],
        update_interval_minutes: int,
        *,
        psu_ip: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}",
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self.client = client
        self.account_uids = account_uids
        self.psu_ip = psu_ip
        self._initial_refresh = True
        self._refresh_lock = asyncio.Lock()

        # Deduplication state (in-memory, resets on HA restart — first poll
        # after a restart seeds the set without firing events, same as
        # fints4's approach).
        self._seen_keys: dict[str, set[str]] = {}
        self._first_run = True

    async def _async_update_data(self) -> EnableBankingCoordinatorData:
        async with self._refresh_lock:
            psu_ip = self.psu_ip if self._initial_refresh else None
            self._initial_refresh = False
            return await self._async_fetch_data(psu_ip=psu_ip)

    async def _async_fetch_data(
        self, *, psu_ip: str | None
    ) -> EnableBankingCoordinatorData:
        result = EnableBankingCoordinatorData()
        date_from = (
            date.today() - timedelta(days=TRANSACTION_LOOKBACK_DAYS)
        ).isoformat()

        for account_uid in self.account_uids:
            account_data = EnableBankingAccountData()

            try:
                account_data.balances = await self.client.get_balances(
                    account_uid, psu_ip=psu_ip
                )
            except EnableBankingSessionExpiredError as err:
                raise ConfigEntryAuthFailed(
                    "Enable Banking consent expired — please re-authorise"
                ) from err
            except EnableBankingAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except EnableBankingRateLimitError as err:
                raise UpdateFailed(f"Rate limited by ASPSP: {err}") from err
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to get balances for %s: %s", account_uid, err)

            try:
                account_data.transactions = await self.client.get_transactions(
                    account_uid, date_from=date_from, strategy="longest", psu_ip=psu_ip
                )
            except EnableBankingSessionExpiredError as err:
                raise ConfigEntryAuthFailed(
                    "Enable Banking consent expired — please re-authorise"
                ) from err
            except EnableBankingRateLimitError as err:
                raise UpdateFailed(f"Rate limited by ASPSP: {err}") from err
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to get transactions for %s: %s", account_uid, err
                )

            result.accounts[account_uid] = account_data
            result.new_transactions[account_uid] = self._deduplicate(
                account_uid, account_data.transactions
            )

        if self._first_run:
            self._first_run = False
            for uid in result.new_transactions:
                result.new_transactions[uid] = []

        return result

    async def async_refresh_balances(self) -> None:
        """Refresh every balance on demand, retaining transaction data.

        Serialize with polling and publish only once all accounts succeeded.
        No transient header is stored on the client or shared HTTP session.
        """
        if not self.psu_ip:
            raise HomeAssistantError(
                "Configure a PSU IP address in the integration options first",
                translation_domain=DOMAIN,
                translation_key="psu_ip_required",
            )

        async with self._refresh_lock:
            accounts = dict(self.data.accounts) if self.data else {}
            try:
                for uid in self.account_uids:
                    balances = await self.client.get_balances(uid, psu_ip=self.psu_ip)
                    accounts[uid] = replace(
                        accounts.get(uid, EnableBankingAccountData()), balances=balances
                    )
            except (EnableBankingSessionExpiredError, EnableBankingAuthError) as err:
                if self.config_entry:
                    self.config_entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    "Enable Banking authorisation failed — please re-authorise",
                    translation_domain=DOMAIN,
                    translation_key="refresh_auth_failed",
                ) from err
            except (EnableBankingError, ClientError, TimeoutError) as err:
                raise HomeAssistantError(
                    f"Could not refresh balances: {err}",
                    translation_domain=DOMAIN,
                    translation_key="refresh_failed",
                ) from err

            # A balance-only update must keep the full transaction poll's deadline.
            self.data = EnableBankingCoordinatorData(accounts=accounts)
            self.last_update_success = True
            self.last_exception = None
            self.async_update_listeners()

    def _deduplicate(
        self, account_uid: str, transactions: list[Transaction]
    ) -> list[Transaction]:
        keys = {tx.dedup_key: tx for tx in transactions}
        prev = self._seen_keys.get(account_uid, set())
        new = [tx for key, tx in keys.items() if key not in prev]
        self._seen_keys[account_uid] = set(keys.keys())
        return new
