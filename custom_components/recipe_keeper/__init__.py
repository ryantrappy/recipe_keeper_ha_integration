"""Recipe Keeper native cloud integration."""

import sqlite3
from pathlib import Path

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed

from ._vendor.recipe_keeper_client.async_client import AsyncRecipeClient
from ._vendor.recipe_keeper_client.cloud_backend import Credentials
from .const import CONF_ALLOW_EDITS, DOMAIN
from .coordinator import RecipeKeeperCoordinator, RecipeKeeperEntry
from .panel import async_setup_panel, async_unload_panel
from .services import async_setup_services, async_unload_services

PLATFORMS = [Platform.CALENDAR, Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass, config) -> bool:
    async_setup_services(hass)
    await async_setup_panel(hass)
    return True


async def async_setup_entry(hass, entry: RecipeKeeperEntry) -> bool:
    async_setup_services(hass)
    await async_setup_panel(hass)
    credentials = Credentials(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    client = AsyncRecipeClient(
        async_get_clientsession(hass),
        credentials,
        Path(hass.config.path(".storage", f"{DOMAIN}_{entry.entry_id}.db")),
        allow_edits=entry.options.get(CONF_ALLOW_EDITS, False),
        executor=hass.async_add_executor_job,
    )
    coordinator = RecipeKeeperCoordinator(hass, entry, client)
    try:
        await client.initialize()
        try:
            await coordinator.async_config_entry_first_refresh()
        except ConfigEntryNotReady:
            cached = await client.cached_status()
            if not cached["cache_ready"]:
                raise
            coordinator.async_set_updated_data(cached)
            coordinator.async_set_update_error(
                UpdateFailed("Using cached data while cloud is unavailable")
            )
    except (OSError, sqlite3.Error, ValueError):
        await client.close()
        raise ConfigEntryNotReady("Recipe Keeper cache unavailable") from None
    except BaseException:
        await client.close()
        raise
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await client.close()
        raise
    return True


async def async_reload_entry(hass, entry: RecipeKeeperEntry) -> None:
    entry.runtime_data.client.transport.allow_edits = False
    entry.runtime_data.importer.stop()
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry: RecipeKeeperEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry.runtime_data.importer.stop()
        await entry.runtime_data.client.close()
        await entry.runtime_data.importer.close()
        if not any(
            item.entry_id != entry.entry_id and hasattr(item, "runtime_data")
            for item in hass.config_entries.async_entries(DOMAIN)
        ):
            async_unload_services(hass)
            async_unload_panel(hass)
        return True
    return False
