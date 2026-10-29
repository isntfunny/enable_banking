"""Manual balance refresh, once per bank integration."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EnableBankingDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a single refresh button for all accounts in the entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([EnableBankingRefreshBalancesButton(coordinator, entry)])


class EnableBankingRefreshBalancesButton(ButtonEntity):
    """Stay available when background polling is rate limited."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh_balances"
    _attr_icon = "mdi:refresh"

    def __init__(
        self, coordinator: EnableBankingDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_refresh_balances"
        self._attr_translation_placeholders = {"bank": entry.title}

    async def async_press(self) -> None:
        """Fetch all account balances with the configured PSU IP."""
        await self.coordinator.async_refresh_balances()
