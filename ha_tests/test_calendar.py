"""All-day calendar boundaries are in HA's timezone and retain cloud UIDs."""

from unittest.mock import AsyncMock

import pytest
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


@pytest.mark.parametrize(
    ("label", "calendar"),
    [
        pytest.param("Breakfast", "breakfast", id="breakfast"),
        pytest.param("Lunch", "lunch", id="lunch"),
        pytest.param(" Dinner ", "dinner", id="trimmed-dinner"),
        pytest.param("SIDE", "side", id="side"),
        pytest.param("Dessert", "dessert", id="dessert"),
        pytest.param("Drink", "drink", id="drink"),
        pytest.param("Snack", "snack", id="snack"),
        pytest.param("Brunch", "other_meals", id="custom-label"),
        pytest.param("", "other_meals", id="unlabelled"),
    ],
)
async def test_each_meal_in_combined_and_one_type_calendar(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    label: str,
    calendar: str,
) -> None:
    client.sync.return_value["calendar_meals"][0]["meal_type"] = label
    assert await hass.config_entries.async_setup(entry.entry_id)
    entity_ids = [
        "calendar.recipe_keeper_" + key
        for key in (
            "breakfast",
            "lunch",
            "dinner",
            "side",
            "dessert",
            "drink",
            "snack",
            "other_meals",
        )
    ]
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": [*entity_ids, "calendar.recipe_keeper_meal_plan"],
            "start_date_time": "2026-10-21T00:00:00+00:00",
            "end_date_time": "2026-10-22T00:00:00+00:00",
        },
        blocking=True,
        return_response=True,
    )
    assert len(result["calendar.recipe_keeper_meal_plan"]["events"]) == 1
    assert len(result["calendar.recipe_keeper_" + calendar]["events"]) == 1
    assert sum(len(result[entity_id]["events"]) for entity_id in entity_ids) == 1
