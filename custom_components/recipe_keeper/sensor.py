"""Compact counts and synchronization time."""

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import RecipeKeeperEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        [
            RecipeKeeperSensor(entry.runtime_data, key)
            for key in ("recipes", "meals", "categories", "last_successful_sync_at")
        ]
    )


class RecipeKeeperSensor(RecipeKeeperEntity, SensorEntity):
    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self.key = key
        self._attr_translation_key = key
        if key == "last_successful_sync_at":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        else:
            self._attr_icon = {
                "recipes": "mdi:book-open-page-variant",
                "meals": "mdi:calendar",
                "categories": "mdi:shape",
            }[key]

    @property
    def native_value(self):
        value = self.coordinator.data.get(self.key)
        return (
            datetime.fromisoformat(value)
            if self.key == "last_successful_sync_at" and value
            else value
        )
