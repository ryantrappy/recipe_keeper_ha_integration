"""Complete, read-only sync exports never confuse a date window with deletion."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from recipe_assistant.api import create_app
from recipe_assistant.cloud_backend import (
    CacheUnavailable,
    CloudCache,
    CloudDatabase,
    SyncWorker,
)
from recipe_assistant.cloud_edits import CloudEditor
from recipe_assistant.cloud_protocol import ProbeError
from recipe_assistant.database import Database
from recipe_assistant.sync_snapshot import read_sync_snapshot

from .conftest import TOKEN
from .test_cloud_backend import CREDENTIALS, CURSOR, MEAL, RECIPE
from .test_cloud_edits import FakeCloud


@pytest.fixture
def database(tmp_path: Path) -> CloudDatabase:
    cache = CloudCache(tmp_path / "cloud.db", CREDENTIALS.account_id)
    cache.apply(
        {
            "Recipe": {
                "r1": {
                    **RECIPE,
                    "LastModified": CURSOR,
                    "Photo": "private-photo",
                    "Unknown": "private-field",
                    "Rating": 4,
                },
                "r2": {
                    **RECIPE,
                    "Id": "r2",
                    "Title": "Unplanned",
                    "LastModified": CURSOR,
                },
                "rd": {"Id": "rd", "IsDeleted": True, "LastModified": CURSOR},
            },
            "Meal": {
                "m1": {
                    **MEAL,
                    "LastModified": CURSOR,
                    "MealType": 3,
                    "MealTime": 100,
                    "Position": 7,
                    "Colour": 42,
                },
                "m_old": {
                    **MEAL,
                    "Id": "m_old",
                    "MealDate": "1999-01-01T00:00:00",
                    "Title": "Unmatched",
                    "LastModified": CURSOR,
                },
                "md": {"Id": "md", "IsDeleted": True, "LastModified": CURSOR},
            },
        },
        {"Recipe": CURSOR, "Meal": CURSOR},
        datetime.now(UTC),
    )
    return CloudDatabase(cache, 300)


def test_full_contract_and_exact_revisions(database: CloudDatabase) -> None:
    result = read_sync_snapshot(database)
    assert result.complete and not result.stale
    assert result.scope == "all_cached_recipes_and_meals"
    assert result.account_id == CREDENTIALS.account_id
    assert [r.id for r in result.recipes] == ["r1", "r2"]
    assert [m.id for m in result.meals] == ["m1", "m_old"]
    assert result.meals[0].recipe.id == "r1"
    assert result.meals[0].recipe.model_dump() == database.recipe("r1").model_dump()
    assert result.meals[0].meal_type_code == 3
    assert result.meals[0].position == 7
    assert result.meals[0].meal_time_raw == 100
    assert result.meals[0].colour_raw == 42
    assert result.meals[1].match == "unmatched"
    assert result.deleted_meals[0].id == "md"
    assert result.deleted_recipes[0].id == "rd"
    editor = CloudEditor(SyncWorker(database.cache, Mock()))
    assert result.meals[0].revision == editor.view("Meal", "m1")["revision"]
    assert result.recipes[0].revision == editor.view("Recipe", "r1")["revision"]
    assert result.recipes[0].last_modified == CURSOR
    exported = result.model_dump_json()
    assert "private-photo" not in exported
    assert "private-field" not in exported
    assert CREDENTIALS.password not in exported
    assert result.allow_edits is False


def test_stable_identity_ignores_poll_time_and_policy(database: CloudDatabase) -> None:
    first = read_sync_snapshot(database)
    database.cache.apply(
        {"Recipe": {}, "Meal": {}},
        {"Recipe": CURSOR, "Meal": CURSOR},
        datetime.now(UTC) + timedelta(seconds=1),
    )
    later = read_sync_snapshot(database, True)
    assert later.snapshot_id == first.snapshot_id
    assert later.last_successful_sync_at != first.last_successful_sync_at
    assert later.allow_edits
    changed = {
        **MEAL,
        "LastModified": CURSOR,
        "Notes": "Changed",
        "MealDate": "2030-01-01T00:00:00",
    }
    database.cache.apply(
        {"Recipe": {}, "Meal": {"m1": changed}},
        {"Recipe": CURSOR, "Meal": CURSOR},
        datetime.now(UTC),
    )
    result = read_sync_snapshot(database)
    assert result.snapshot_id != later.snapshot_id
    assert result.meals[0].id == "m1"
    assert [m.id for m in result.deleted_meals] == ["md"]


def test_ambiguity_is_explicit(database: CloudDatabase) -> None:
    database.cache.apply(
        {"Recipe": {"r2": {**RECIPE, "Id": "r2", "LastModified": CURSOR}}, "Meal": {}},
        {"Recipe": CURSOR, "Meal": CURSOR},
        datetime.now(UTC),
    )
    result = read_sync_snapshot(database)
    assert result.meals[0].recipe is None
    assert result.meals[0].match == "ambiguous"
    assert result.meals[0].candidate_recipe_ids == ["r1", "r2"]


def test_unresolved_operations_are_visible_without_payloads(
    database: CloudDatabase,
) -> None:
    cloud = FakeCloud()
    cloud.apply_upload = False
    worker = SyncWorker(database.cache, cloud)
    worker.sync()
    operation_id = str(uuid4())
    CloudEditor(worker, allow_edits=True).edit(
        "Recipe", "create", operation_id, {"title": "Synthetic held recipe"}
    )
    snapshot = read_sync_snapshot(database)
    assert snapshot.stale is False
    assert snapshot.unresolved_operations[0].operation_id == operation_id
    assert snapshot.unresolved_operations[0].state == "uncertain"
    assert "Synthetic held recipe" not in snapshot.model_dump_json()
    assert "Baseline" not in snapshot.model_dump_json()


@pytest.mark.parametrize("missing", ["raw_ready", "raw_row", "projection", "modified"])
def test_incomplete_snapshot_fails_closed(
    database: CloudDatabase, missing: str
) -> None:
    commands = {
        "raw_ready": "DELETE FROM Metadata WHERE key='raw_snapshot_ready'",
        "raw_row": "DELETE FROM RawRecords WHERE Section='Recipe' AND Id='r1'",
        "projection": "DELETE FROM Recipes WHERE Id='r1'",
        "modified": (
            "UPDATE RawRecords SET Record=json_remove(Record,'$.LastModified') "
            "WHERE Id='r1'"
        ),
    }
    with database.cache.connect() as conn:
        conn.execute(commands[missing])
    with pytest.raises((CacheUnavailable, ProbeError)):
        read_sync_snapshot(database)


def test_api_authentication_and_response(database: CloudDatabase) -> None:
    with TestClient(create_app(database, TOKEN)) as client:
        assert client.get("/v1/sync-snapshot").status_code == 401
        response = client.get(
            "/v1/sync-snapshot", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        assert response.json()["schema_version"] == 1
        assert response.headers["Cache-Control"] == "no-store"
        assert (
            "SyncSnapshot"
            in client.get("/openapi.json").json()["components"]["schemas"]
        )


def test_local_backend_does_not_claim_cloud_completeness(
    database: CloudDatabase,
) -> None:
    with TestClient(create_app(Database(database.path), TOKEN)) as client:
        response = client.get(
            "/v1/sync-snapshot", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 503


def test_export_uses_one_sqlite_read_transaction(
    database: CloudDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = read_sync_snapshot(database)
    original = database._recipe

    def concurrent_write(conn, recipe_id: str):
        if recipe_id == "r1":
            database.cache.apply(
                {
                    "Recipe": {
                        "r1": {
                            **RECIPE,
                            "LastModified": CURSOR,
                            "Notes": "Concurrent recipe",
                        }
                    },
                    "Meal": {
                        "m1": {
                            **MEAL,
                            "LastModified": CURSOR,
                            "MealDate": "2030-02-01T00:00:00",
                        }
                    },
                },
                {"Recipe": CURSOR, "Meal": CURSOR},
                datetime.now(UTC) + timedelta(seconds=1),
            )
        return original(conn, recipe_id)

    monkeypatch.setattr(database, "_recipe", concurrent_write)
    exported = read_sync_snapshot(database)
    assert exported.snapshot_id == initial.snapshot_id
    assert exported.last_successful_sync_at == initial.last_successful_sync_at
    assert exported.recipes[0].notes == initial.recipes[0].notes
    assert exported.meals[0].date == initial.meals[0].date
    monkeypatch.setattr(database, "_recipe", original)
    assert database.recipe("r1").notes == "Concurrent recipe"


@pytest.mark.asyncio
async def test_async_client_export_needs_no_cloud_call(database: CloudDatabase) -> None:
    from recipe_assistant.async_client import AsyncRecipeClient

    transport = Mock()
    client = AsyncRecipeClient(transport, CREDENTIALS, database.path)
    await client.initialize()
    try:
        exported = await client.sync_snapshot()
        assert exported["account_id"] == CREDENTIALS.account_id
        assert exported["meals"][0]["id"] == "m1"
        transport.post.assert_not_called()
    finally:
        await client.close()
