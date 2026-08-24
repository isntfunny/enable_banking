"""Enable Banking event entity: fires on each newly seen (booked) transaction."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import AccountDetails, Transaction
from .const import DOMAIN, EVENT_NEW_TRANSACTION
from .coordinator import EnableBankingDataUpdateCoordinator, get_account_device_info

_LOGGER = logging.getLogger(__name__)


def _event_payload(tx: Transaction) -> dict[str, Any]:
    return {
        "amount": tx.signed_amount,
        "currency": tx.transaction_amount.currency,
        "date": tx.value_date or tx.booking_date,
        "status": tx.status,
        "remittance_information": tx.remittance_information,
        "creditor_name": tx.creditor_name,
        "debtor_name": tx.debtor_name,
        "bank_transaction_code": tx.bank_transaction_code,
        "entry_reference": tx.entry_reference,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Enable Banking event entities from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: EnableBankingDataUpdateCoordinator = entry_data["coordinator"]
    account_uids: list[str] = entry_data["account_uids"]
    account_details: dict[str, AccountDetails] = entry_data["account_details"]

    entities = [
        EnableBankingNewTransactionEvent(
            coordinator, entry, uid, account_details.get(uid)
        )
        for uid in account_uids
    ]
    async_add_entities(entities)


class EnableBankingNewTransactionEvent(
    CoordinatorEntity[EnableBankingDataUpdateCoordinator], EventEntity
):
    """Fires once per newly observed transaction (booked or pending)."""

    _attr_icon = "mdi:bank-transfer"
    _attr_event_types = [EVENT_NEW_TRANSACTION]

    def __init__(
        self,
        coordinator: EnableBankingDataUpdateCoordinator,
        entry: ConfigEntry,
        account_uid: str,
        details: AccountDetails | None,
    ) -> None:
        super().__init__(coordinator)
        self._account_uid = account_uid
        name = (details.product if details else None) or account_uid
        self._attr_unique_id = f"{entry.entry_id}_{account_uid}_new_transaction"
        self._attr_name = f"{name} New Transaction"
        self._attr_device_info = get_account_device_info(entry, account_uid, details)

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self.coordinator.data:
            return

        new_txs: list[Transaction] = self.coordinator.data.new_transactions.get(
            self._account_uid, []
        )
        if not new_txs:
            return  # nothing new — skip the state write

        for tx in new_txs:
            self._trigger_event(EVENT_NEW_TRANSACTION, _event_payload(tx))

        self.async_write_ha_state()
