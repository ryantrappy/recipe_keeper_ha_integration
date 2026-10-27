"""Mealie-style actions through Home Assistant's service registry."""

from datetime import date
from unittest.mock import AsyncMock
from uuid import uuid4

import probatio as vol
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def action(hass: HomeAssistant, name: str, data: dict) -> dict:
    return await hass.services.async_call(
        "recipe_keeper", name, data, blocking=True, return_response=True
    )


async def test_read_aliases_include_revisions_and_ha_local_today(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-28T01:00:00+00:00")
    await hass.config.async_update(time_zone="America/Chicago")
    client.search.return_value = {"items": [{"id": "r1"}], "total": 1}
    client.mealplans.return_value = {
        "mealplan": [{"id": "m1", "revision": "a" * 64}],
        "stale": True,
        "last_successful_sync_at": "2026-10-20T12:00:01+00:00",
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await action(
        hass,
        "get_recipes",
        {
            "config_entry_id": entry.entry_id,
            "search_terms": "carrot",
            "result_limit": 12,
            "offset": 3,
        },
    )
    assert result["recipes"]["items"] == [{"id": "r1"}]
    client.search.assert_awaited_once_with("carrot", 12, 3)
    result = await action(hass, "get_mealplan", {"config_entry_id": entry.entry_id})
    client.mealplans.assert_awaited_once_with(date(2026, 10, 27), date(2026, 10, 27))
    assert result["mealplan"][0]["revision"] == "a" * 64
    assert result["stale"] is True
    assert result["last_successful_sync_at"] == "2026-10-20T12:00:01+00:00"


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"recipe_id": "r1", "note_title": "Both"}, id="both-sources"),
        pytest.param({}, id="missing-source"),
        pytest.param({"recipe_id": ""}, id="empty-id"),
    ],
)
async def test_plan_requires_exactly_one_source(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock, fields: dict
) -> None:
    hass.config_entries.async_update_entry(entry, options={"allow_edits": True})
    client.allow_edits = True
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(vol.Invalid):
        await action(
            hass,
            "set_mealplan",
            {
                "config_entry_id": entry.entry_id,
                "operation_id": str(uuid4()),
                "date": "2026-10-28",
                "entry_type": "dinner",
                **fields,
            },
        )
    client.plan_meal.assert_not_awaited()


@pytest.mark.parametrize(
    ("name", "fields", "expected"),
    [
        pytest.param(
            "set_mealplan",
            {"recipe_id": "r1"},
            {"recipe_id": "r1", "note_title": None, "note_text": None},
            id="recipe",
        ),
        pytest.param(
            "set_mealplan",
            {"note_title": "Eat out", "note_text": "Friends"},
            {"recipe_id": None, "note_title": "Eat out", "note_text": "Friends"},
            id="note",
        ),
        pytest.param(
            "set_random_mealplan",
            {},
            {"search_terms": "", "favourite_only": False},
            id="random",
        ),
        pytest.param(
            "update_mealplan",
            {"recipe_id": "r1", "mealplan_id": "m1", "revision": "b" * 64},
            {
                "recipe_id": "r1",
                "note_title": None,
                "note_text": None,
                "mealplan_id": "m1",
                "revision": "b" * 64,
            },
            id="update",
        ),
    ],
)
async def test_plan_actions_are_gated_and_forward_durable_intent(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    name: str,
    fields: dict,
    expected: dict,
) -> None:
    operation_id = str(uuid4())
    data = {
        "config_entry_id": entry.entry_id,
        "operation_id": operation_id,
        "date": "2026-10-28",
        "entry_type": "dinner",
        **fields,
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError, match="Enable Allow editing"):
        await action(hass, name, data)
    client.plan_meal.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, options={"allow_edits": True})
    client.allow_edits = True
    client.plan_meal.return_value = {
        "operation_id": operation_id,
        "state": "confirmed",
        "mealplan_id": "m2",
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await action(hass, name, data)
    assert result["mealplan_id"] == "m2"
    client.plan_meal.assert_awaited_once_with(
        operation_id,
        {"action": name, "date": "2026-10-28", "entry_type": "dinner", **expected},
    )


async def test_delete_mealplan_requires_current_revision(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    hass.config_entries.async_update_entry(entry, options={"allow_edits": True})
    client.allow_edits = True
    client.edit.return_value = {"entity_id": "m1", "state": "confirmed"}
    assert await hass.config_entries.async_setup(entry.entry_id)
    operation_id = str(uuid4())
    response = await action(
        hass,
        "delete_mealplan",
        {
            "config_entry_id": entry.entry_id,
            "operation_id": operation_id,
            "mealplan_id": "m1",
            "revision": "b" * 64,
        },
    )
    assert response["mealplan_id"] == "m1"
    client.edit.assert_awaited_once_with(
        "Meal", "delete", operation_id, {}, entity_id="m1", expected_revision="b" * 64
    )


async def test_import_recipe_options_and_gate(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    data = {
        "config_entry_id": entry.entry_id,
        "operation_id": str(uuid4()),
        "url": "https://example.com/recipe",
        "include_tags": True,
        "recipe_index": 1,
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError, match="Enable Allow editing"):
        await action(hass, "import_recipe", data)
    client.import_recipe.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, options={"allow_edits": True})
    client.allow_edits = True
    client.import_recipe.return_value = {"state": "confirmed", "entity_id": "r2"}
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert (await action(hass, "import_recipe", data))["entity_id"] == "r2"
    client.import_recipe.assert_awaited_once_with(
        data["operation_id"], data["url"], include_tags=True, recipe_index=1
    )


@pytest.mark.parametrize(
    ("name", "fields"),
    [
        pytest.param(
            "set_mealplan",
            {"date": "2026-10-28", "entry_type": "dinner", "recipe_id": "r1"},
            id="set",
        ),
        pytest.param(
            "set_random_mealplan",
            {"date": "2026-10-28", "entry_type": "dinner"},
            id="random",
        ),
        pytest.param(
            "update_mealplan",
            {
                "date": "2026-10-28",
                "entry_type": "dinner",
                "recipe_id": "r1",
                "mealplan_id": "m1",
                "revision": "b" * 64,
            },
            id="update",
        ),
        pytest.param(
            "delete_mealplan", {"mealplan_id": "m1", "revision": "b" * 64}, id="delete"
        ),
        pytest.param(
            "import_recipe", {"url": "https://example.com/recipe"}, id="import"
        ),
    ],
)
async def test_new_writes_require_admin(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    hass_read_only_user: User,
    name: str,
    fields: dict,
) -> None:
    hass.config_entries.async_update_entry(entry, options={"allow_edits": True})
    client.allow_edits = True
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "recipe_keeper",
            name,
            {"config_entry_id": entry.entry_id, "operation_id": str(uuid4()), **fields},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    client.plan_meal.assert_not_awaited()
    client.import_recipe.assert_not_awaited()
    client.edit.assert_not_awaited()


async def test_shopping_read_uses_selected_ha_list_and_caller_context(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    hass_admin_user: User,
) -> None:
    hass.config_entries.async_update_entry(
        entry, options={"allow_edits": False, "todo_entity": "todo.groceries"}
    )
    get_items = AsyncMock(
        return_value={
            "todo.groceries": {
                "items": [{"uid": "i1", "summary": "Carrots", "status": "needs_action"}]
            }
        }
    )
    hass.services.async_register(
        "todo", "get_items", get_items, supports_response=SupportsResponse.ONLY
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    context = Context(user_id=hass_admin_user.id)
    result = await hass.services.async_call(
        "recipe_keeper",
        "get_shopping_list_items",
        {"config_entry_id": entry.entry_id},
        blocking=True,
        return_response=True,
        context=context,
    )
    assert result["source"] == "home_assistant"
    assert result["items"][0]["summary"] == "Carrots"
    call = get_items.await_args.args[0]
    assert call.context is context
    assert call.data == {"entity_id": "todo.groceries", "status": ["needs_action"]}
    client.edit.assert_not_awaited()


async def test_services_removed_on_unload(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert hass.services.has_service("recipe_keeper", "import_recipe")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service("recipe_keeper", "import_recipe")


async def test_missing_shopping_entity_reports_validation_error(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
) -> None:
    get_items = AsyncMock(return_value={})
    hass.services.async_register(
        "todo", "get_items", get_items, supports_response=SupportsResponse.ONLY
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError, match="Selected to-do list is unavailable"):
        await action(
            hass,
            "get_shopping_list_items",
            {
                "config_entry_id": entry.entry_id,
                "todo_entity": "todo.missing",
            },
        )
