"""All-day calendar boundaries are in HA's timezone and retain cloud UIDs."""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_calendar_local_dates(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    await hass.config.async_update(time_zone="America/Chicago")
    assert await hass.config_entries.async_setup(entry.entry_id)
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.recipe_keeper_meal_plan",
            "start_date_time": "2026-10-21T05:00:00+00:00",
            "end_date_time": "2026-10-22T05:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )
    events = response["calendar.recipe_keeper_meal_plan"]["events"]
    assert len(events) == 1
    assert events[0]["start"] == "2026-10-21"
    assert events[0]["end"] == "2026-10-22"
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.recipe_keeper_meal_plan",
            "start_date_time": "2026-10-20T05:00:00+00:00",
            "end_date_time": "2026-10-21T05:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )
    assert response["calendar.recipe_keeper_meal_plan"]["events"] == []
