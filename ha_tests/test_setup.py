"""Entry lifecycle and default entity behavior."""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.recipe_keeper._vendor.recipe_keeper_client import cloud_protocol


async def test_setup_and_unload(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.recipe_keeper_recipes").state == "1"
    assert hass.states.get("calendar.recipe_keeper_meal_plan") is not None
    assert hass.states.get("binary_sensor.recipe_keeper_editing_enabled").state == "off"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    client.close.assert_awaited_once()


async def test_offline_cache_and_retry(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    cached = {**client.sync.return_value, "cache_ready": True}
    client.sync.side_effect = cloud_protocol.ProbeError("synthetic offline")
    client.cached_status.return_value = cached
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert hass.states.get("sensor.recipe_keeper_recipes").state == "unavailable"
    client.search.return_value = {"items": [], "total": 0}
    response = await hass.services.async_call(
        "recipe_keeper",
        "search_recipes",
        {"config_entry_id": entry.entry_id},
        blocking=True,
        return_response=True,
    )
    assert response["stale"] is True
    assert response["total"] == 0
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_initial_sync_failure_closes_client(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    client.sync.side_effect = cloud_protocol.ProbeError("synthetic offline")
    client.cached_status.return_value = {"cache_ready": False}
    assert not await hass.config_entries.async_setup(entry.entry_id)
    client.close.assert_awaited_once()


async def test_frontend_registration_and_cleanup(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "frontend", {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert "recipe-keeper" in hass.data["frontend_panels"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "recipe-keeper" not in hass.data["frontend_panels"]
