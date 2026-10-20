"""Diagnostic indicators without mutable edit switches."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .const import CONF_ALLOW_EDITS
from .entity import RecipeKeeperEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        [
            RecipeKeeperBinarySensor(entry.runtime_data, key)
            for key in ("cloud_data_fresh", "editing_enabled")
        ]
    )


class RecipeKeeperBinarySensor(RecipeKeeperEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self.key = key
        self._attr_translation_key = key

    @property
    def is_on(self) -> bool:
        return (
            self.coordinator.is_fresh
            if self.key == "cloud_data_fresh"
            else self.coordinator.entry.options.get(CONF_ALLOW_EDITS, False)
        )
