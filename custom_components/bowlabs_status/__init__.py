"""Installable, status-only BowLabs synthetic pilot."""

from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, PLATFORMS
from .manager import get_manager
from .views import StatusView

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass, config):
    hass.http.register_view(StatusView(hass))
    return True


async def async_setup_entry(hass, entry):
    manager = get_manager(hass)
    if entry.data.get("kind") == "receiver":
        manager.enabled = True
        manager.receiver_id = entry.entry_id

        @callback
        def tick(now):
            manager.link.tick()

        entry.async_on_unload(
            async_track_time_interval(hass, tick, timedelta(seconds=5))
        )
        return True
    entry.runtime_data = manager.coordinator(entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        manager.bind(entry)
    except BaseException:
        manager.revoke_entry(entry.entry_id)
        manager.coordinators.pop(entry.entry_id, None)
        raise
    return True


async def async_unload_entry(hass, entry):
    manager = get_manager(hass)
    if entry.data.get("kind") == "receiver":
        manager.disable()
        manager.receiver_id = None
        return True
    # Revoke BEFORE awaiting unload so in-flight requests cannot commit even
    # when a sensor unload fails. Removal/disable/reload all fail closed.
    manager.revoke_entry(entry.entry_id)
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if result:
        manager.coordinators.pop(entry.entry_id, None)
    return result


async def async_remove_entry(hass, entry):
    manager = get_manager(hass)
    if entry.data.get("kind") == "receiver":
        manager.disable()
    else:
        manager.revoke_entry(entry.entry_id)
