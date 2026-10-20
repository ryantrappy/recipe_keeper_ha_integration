"""Public actions, edit gates, input contracts, and content-free diagnostics."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.recipe_keeper.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def action(hass: HomeAssistant, name: str, data: dict) -> dict:
    return await hass.services.async_call(
        "recipe_keeper", name, data, blocking=True, return_response=True
    )


async def test_read_actions_and_diagnostics(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    client.search.return_value = {"items": [{"id": "r1", "title": "Soup"}], "total": 1}
    client.recipe.return_value = {
        "id": "r1",
        "ingredients": "2 carrots",
        "directions": "Boil.",
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await action(hass, "get_accounts", {})
    assert result["accounts"][0]["allow_edits"] is False
    result = await action(
        hass, "search_recipes", {"config_entry_id": entry.entry_id, "query": "soup"}
    )
    assert result["total"] == 1
    result = await action(
        hass, "get_recipe", {"config_entry_id": entry.entry_id, "recipe_id": "r1"}
    )
    assert result["recipe"]["ingredients"] == "2 carrots"
    diagnostics = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    assert "Soup" not in diagnostics
    assert "synthetic-password" not in diagnostics
    assert "example.invalid" not in diagnostics
    assert "calendar_meals" not in diagnostics
    assert "carrots" not in json.dumps(
        [state.as_dict() for state in hass.states.async_all()]
    )


@pytest.mark.parametrize(
    ("name", "fields", "kind", "operation", "changes", "entity_id", "revision"),
    [
        pytest.param(
            "create_recipe",
            {"changes": {"title": "Disposable"}},
            "Recipe",
            "create",
            {"title": "Disposable"},
            None,
            None,
            id="create-recipe",
        ),
        pytest.param(
            "update_recipe",
            {"recipe_id": "r1", "revision": "a" * 64, "changes": {"notes": "New"}},
            "Recipe",
            "update",
            {"notes": "New"},
            "r1",
            "a" * 64,
            id="update-recipe",
        ),
        pytest.param(
            "delete_recipe",
            {"recipe_id": "r1", "revision": "a" * 64},
            "Recipe",
            "delete",
            {},
            "r1",
            "a" * 64,
            id="delete-recipe",
        ),
        pytest.param(
            "create_meal",
            {"changes": {"title": "Soup", "date": "2026-10-21"}},
            "Meal",
            "create",
            {"title": "Soup", "date": "2026-10-21"},
            None,
            None,
            id="create-meal",
        ),
        pytest.param(
            "update_meal",
            {"meal_id": "m1", "revision": "a" * 64, "changes": {"notes": "New"}},
            "Meal",
            "update",
            {"notes": "New"},
            "m1",
            "a" * 64,
            id="update-meal",
        ),
        pytest.param(
            "delete_meal",
            {"meal_id": "m1", "revision": "a" * 64},
            "Meal",
            "delete",
            {},
            "m1",
            "a" * 64,
            id="delete-meal",
        ),
    ],
)
async def test_edit_actions_opt_in(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    name: str,
    fields: dict,
    kind: str,
    operation: str,
    changes: dict,
    entity_id: str | None,
    revision: str | None,
) -> None:
    operation_id = str(uuid4())
    data = {"config_entry_id": entry.entry_id, "operation_id": operation_id, **fields}
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError, match="Enable Allow editing"):
        await action(hass, name, data)
    client.edit.assert_not_awaited()
    # Set options before a fresh setup so the real update listener is not bypassed.
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, options={"allow_edits": True})
    client.allow_edits = True
    client.edit.return_value = {"operation_id": operation_id, "state": "confirmed"}
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert (await action(hass, name, data))["state"] == "confirmed"
    client.edit.assert_awaited_once_with(
        kind,
        operation,
        operation_id,
        changes,
        entity_id=entity_id,
        expected_revision=revision,
    )


async def test_validation_and_disabled_reconciliation(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError):
        await action(
            hass,
            "get_meal_plan",
            {
                "config_entry_id": entry.entry_id,
                "start_date": "2026-10-22",
                "end_date": "2026-10-20",
            },
        )
    client.meals.assert_not_awaited()
    client.reconcile.return_value = {"state": "confirmed"}
    operation_id = str(uuid4())
    assert (
        await action(
            hass,
            "reconcile_edit",
            {"config_entry_id": entry.entry_id, "operation_id": operation_id},
        )
    )["state"] == "confirmed"
    client.reconcile.assert_awaited_once_with(operation_id)


async def test_non_admin_cannot_import_or_edit(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock, hass_read_only_user
) -> None:
    from homeassistant.core import Context

    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "recipe_keeper",
            "create_recipe",
            {
                "config_entry_id": entry.entry_id,
                "operation_id": str(uuid4()),
                "changes": {"title": "Blocked"},
            },
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    client.edit.assert_not_awaited()


async def test_sync_snapshot_preserves_own_freshness_metadata(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    client.sync_snapshot.return_value = {
        "schema_version": 1,
        "scope": "all_cached_recipes_and_meals",
        "complete": True,
        "stale": False,
        "snapshot_id": "snapshot",
        "account_id": "opaque-account",
        "recipes": [],
        "meals": [],
        "deleted_recipes": [],
        "deleted_meals": [{"id": "removed-outside-window"}],
        "unresolved_operations": [],
        "last_successful_sync_at": "2026-10-20T12:00:01+00:00",
    }
    client.sync.return_value["last_successful_sync_at"] = datetime.now(UTC).isoformat()
    assert await hass.config_entries.async_setup(entry.entry_id)
    response = await action(
        hass, "get_sync_snapshot", {"config_entry_id": entry.entry_id}
    )
    assert response["complete"] is True
    assert response["deleted_meals"][0]["id"] == "removed-outside-window"
    assert response["last_successful_sync_at"] == "2026-10-20T12:00:01+00:00"
    assert response["stale"] is False
    entry.runtime_data.async_set_update_error(UpdateFailed("synthetic offline"))
    degraded = await action(
        hass, "get_sync_snapshot", {"config_entry_id": entry.entry_id}
    )
    assert degraded["stale"] is True
    assert degraded["complete"] is True
    assert client.sync_snapshot.await_count == 2
    client.edit.assert_not_awaited()
