"""DataUpdateCoordinator for the Enable Banking integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    AccountBalance,
    AccountDetails,
    EnableBankingAuthError,
    EnableBankingClient,
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

    Deliberately never sends Psu-Ip-Address / Psu-User-Agent headers — this
    is a background job, not a PSU-present interaction — so the ASPSP's
    "max 4x/day without PSU" limit (PSD2 RTS Art. 36(6)) applies. The default
    update_interval (const.DEFAULT_UPDATE_INTERVAL = 240 min) is chosen to
    match that exactly.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: EnableBankingClient,
        account_uids: list[str],
        update_interval_minutes: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}",
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self.client = client
        self.account_uids = account_uids

        # Deduplication state (in-memory, resets on HA restart — first poll
        # after a restart seeds the set without firing events, same as
        # fints4's approach).
        self._seen_keys: dict[str, set[str]] = {}
        self._first_run = True

    async def _async_update_data(self) -> EnableBankingCoordinatorData:
        result = EnableBankingCoordinatorData()
        date_from = (date.today() - timedelta(days=TRANSACTION_LOOKBACK_DAYS)).isoformat()

        for account_uid in self.account_uids:
            account_data = EnableBankingAccountData()

            try:
                account_data.balances = await self.client.get_balances(account_uid)
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
                    account_uid, date_from=date_from, strategy="longest"
                )
            except EnableBankingSessionExpiredError as err:
                raise ConfigEntryAuthFailed(
                    "Enable Banking consent expired — please re-authorise"
                ) from err
            except EnableBankingRateLimitError as err:
                raise UpdateFailed(f"Rate limited by ASPSP: {err}") from err
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to get transactions for %s: %s", account_uid, err)

            result.accounts[account_uid] = account_data
            result.new_transactions[account_uid] = self._deduplicate(
                account_uid, account_data.transactions
            )

        if self._first_run:
            self._first_run = False
            for uid in result.new_transactions:
                result.new_transactions[uid] = []

        return result

    def _deduplicate(
        self, account_uid: str, transactions: list[Transaction]
    ) -> list[Transaction]:
        keys = {tx.dedup_key: tx for tx in transactions}
        prev = self._seen_keys.get(account_uid, set())
        new = [tx for key, tx in keys.items() if key not in prev]
        self._seen_keys[account_uid] = set(keys.keys())
        return new
