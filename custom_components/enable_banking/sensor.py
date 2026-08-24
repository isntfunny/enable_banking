"""Enable Banking sensor entities: balance and pending transactions."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .client import AccountBalance, AccountDetails
from .const import (
    ATTR_ACCOUNT_TYPE,
    ATTR_BANK,
    ATTR_BIC,
    ATTR_IBAN,
    ATTR_PRODUCT,
    ACCOUNT_TYPE_BALANCE,
    ACCOUNT_TYPE_CONSENT,
    BALANCE_TYPE_PRIORITY,
    CONF_VALID_UNTIL,
    DOMAIN,
)
from .coordinator import EnableBankingDataUpdateCoordinator, get_account_device_info

_LOGGER = logging.getLogger(__name__)

ICON = "mdi:currency-eur"


def _pick_primary_balance(balances: list[AccountBalance]) -> AccountBalance | None:
    """Pick the "best" balance when an ASPSP returns several balance_types."""
    if not balances:
        return None
    by_type = {b.balance_type: b for b in balances}
    for preferred in BALANCE_TYPE_PRIORITY:
        if preferred in by_type:
            return by_type[preferred]
    return balances[0]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enable Banking sensors from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: EnableBankingDataUpdateCoordinator = entry_data["coordinator"]
    account_uids: list[str] = entry_data["account_uids"]
    account_details: dict[str, AccountDetails] = entry_data["account_details"]

    entities: list[SensorEntity] = []
    for uid in account_uids:
        details = account_details.get(uid)
        entities.append(EnableBankingBalanceSensor(coordinator, entry, uid, details))
        entities.append(
            EnableBankingPendingTransactionsSensor(coordinator, entry, uid, details)
        )

    entities.append(EnableBankingConsentExpirySensor(entry))

    async_add_entities(entities)


class _BaseAccountSensor(
    CoordinatorEntity[EnableBankingDataUpdateCoordinator], SensorEntity
):
    """Shared plumbing for account-scoped sensors."""

    def __init__(
        self,
        coordinator: EnableBankingDataUpdateCoordinator,
        entry: ConfigEntry,
        account_uid: str,
        details: AccountDetails | None,
    ) -> None:
        super().__init__(coordinator)
        self._account_uid = account_uid
        self._details = details
        self._attr_device_info = get_account_device_info(entry, account_uid, details)
        self._base_attrs: dict[str, Any] = {"account_uid": account_uid}
        if details:
            self._base_attrs[ATTR_IBAN] = details.iban
            self._base_attrs[ATTR_BIC] = details.bic
            self._base_attrs[ATTR_PRODUCT] = details.product
            self._base_attrs[ATTR_BANK] = details.product or details.name

    @property
    def _account_data(self):
        if self.coordinator.data:
            return self.coordinator.data.accounts.get(self._account_uid)
        return None

    @property
    def _display_name(self) -> str:
        return (self._details.product if self._details else None) or self._account_uid


class EnableBankingBalanceSensor(_BaseAccountSensor):
    """Primary account balance (picks the preferred balance_type, see const)."""

    _attr_icon = ICON
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: EnableBankingDataUpdateCoordinator,
        entry: ConfigEntry,
        account_uid: str,
        details: AccountDetails | None,
    ) -> None:
        super().__init__(coordinator, entry, account_uid, details)
        self._attr_unique_id = f"{entry.entry_id}_{account_uid}_balance"
        self._attr_name = f"{self._display_name} Balance"
        self._base_attrs[ATTR_ACCOUNT_TYPE] = ACCOUNT_TYPE_BALANCE

    @property
    def _primary_balance(self) -> AccountBalance | None:
        ad = self._account_data
        if not ad:
            return None
        return _pick_primary_balance(ad.balances)

    @property
    def available(self) -> bool:
        return self._primary_balance is not None

    @property
    def native_value(self) -> float | None:
        balance = self._primary_balance
        return balance.balance_amount.as_float if balance else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        balance = self._primary_balance
        return balance.balance_amount.currency if balance else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(self._base_attrs)
        balance = self._primary_balance
        if balance:
            attrs["balance_type"] = balance.balance_type
            attrs["reference_date"] = balance.reference_date
            attrs["last_change_date_time"] = balance.last_change_date_time
        ad = self._account_data
        if ad:
            # Expose every balance_type the ASPSP returned, not just the
            # primary one — some banks report booked vs. available separately.
            attrs["all_balances"] = [
                {
                    "type": b.balance_type,
                    "amount": b.balance_amount.amount,
                    "currency": b.balance_amount.currency,
                }
                for b in ad.balances
            ]
        return attrs


class EnableBankingPendingTransactionsSensor(_BaseAccountSensor):
    """Sum of not-yet-booked (PDNG) transactions in the lookback window."""

    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        coordinator: EnableBankingDataUpdateCoordinator,
        entry: ConfigEntry,
        account_uid: str,
        details: AccountDetails | None,
    ) -> None:
        super().__init__(coordinator, entry, account_uid, details)
        self._attr_unique_id = f"{entry.entry_id}_{account_uid}_pending_transactions"
        self._attr_name = f"{self._display_name} Pending Transactions"

    @property
    def _pending(self) -> list:
        ad = self._account_data
        if not ad:
            return []
        return [tx for tx in ad.transactions if tx.status == "PDNG"]

    @property
    def available(self) -> bool:
        return self._account_data is not None

    @property
    def native_value(self) -> float | None:
        pending = self._pending
        if not pending:
            return 0
        return round(sum(tx.signed_amount for tx in pending), 2)

    @property
    def native_unit_of_measurement(self) -> str | None:
        pending = self._pending
        if pending:
            return pending[0].transaction_amount.currency
        ad = self._account_data
        balance = _pick_primary_balance(ad.balances) if ad else None
        return balance.balance_amount.currency if balance else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(self._base_attrs)
        pending = self._pending
        attrs["count"] = len(pending)
        attrs["transactions"] = [
            {
                "date": tx.value_date or tx.booking_date,
                "amount": tx.signed_amount,
                "currency": tx.transaction_amount.currency,
                "remittance_information": tx.remittance_information,
                "creditor_name": tx.creditor_name,
                "debtor_name": tx.debtor_name,
            }
            for tx in pending
        ]
        return attrs


class EnableBankingConsentExpirySensor(SensorEntity):
    """When the PSD2 consent lapses and a fresh bank login becomes necessary.

    Not a coordinator entity: the value changes only when the entry is
    re-authorised, and it must stay readable even while polling is failing —
    which is exactly the situation where you want to look at it.

    Exposed as a timestamp so it can drive an automation ("remind me a week
    before"), complementing the repairs issue raised during setup.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:calendar-clock"
    _attr_has_entity_name = True
    _attr_translation_key = "consent_expires"
    # The value only ever changes when the entry is re-authorised, and that
    # reloads the entry (recreating this entity), so polling it is pointless.
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_consent_expires"

    @property
    def native_value(self):
        raw = self._entry.data.get(CONF_VALID_UNTIL)
        if not raw:
            return None
        return dt_util.parse_datetime(raw)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {ATTR_ACCOUNT_TYPE: ACCOUNT_TYPE_CONSENT}
