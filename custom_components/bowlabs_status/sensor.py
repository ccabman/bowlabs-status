"""Push-only diagnostic sensors. No service or device controls."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, KEYS


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([StatusSensor(entry, key) for key in KEYS])


class StatusSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry, key):
        super().__init__(entry.runtime_data)
        self.key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = {
            "connection": "Connection",
            "photos": "Photos",
            "calendar": "Calendar",
            "music": "Music permission",
            "album": "Album sample",
        }[key]
        self._attr_icon = {
            "connection": "mdi:lan-connect",
            "photos": "mdi:image-multiple-outline",
            "calendar": "mdi:calendar-outline",
            "music": "mdi:music-note-outline",
            "album": "mdi:image-check-outline",
        }[key]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="BowLabs Mac status",
            manufacturer="BowLabs",
            model="Synthetic status pilot",
        )

    @property
    def native_value(self):
        return self.coordinator.data[self.key]["state"]

    @property
    def extra_state_attributes(self):
        return {
            key: value
            for key, value in self.coordinator.data[self.key].items()
            if key != "state"
        }
