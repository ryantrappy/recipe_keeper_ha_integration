"""Cloud cache and renewal tests use synthetic data and no real credentials."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from recipe_assistant import cloud_backend
from recipe_assistant.api import create_app
from recipe_assistant.cloud_backend import (
    CacheUnavailable,
    CloudCache,
    CloudDatabase,
    CloudSession,
    Credentials,
    SyncBusy,
    SyncWorker,
    load_credentials,
)
from recipe_assistant.cloud_probe import (
    INITIAL_CURSOR,
    Authentication,
    AuthenticationError,
    CloudHTTPError,
    Page,
    ProbeError,
)
from recipe_assistant.database import Database, SchemaError

from .conftest import TOKEN
from .test_cloud_probe import response_section

CURSOR = "2026-10-19T21:01:20.9752375Z"
NEXT_CURSOR = "2026-10-19T21:01:20.9752376Z"
CREDENTIALS = Credentials("test@example.invalid", "synthetic-private-password")
RECIPE = {
    "Id": "r1",
    "Title": "Lemon Soup",
    "Ingredients": "1 lemon\n2 cups water",
    "ServingSize": "4",
    "Directions": "Simmer.",
    "Categories": ["Dinner"],
    "LinkedRecipeIds": [],
    "IsFavourite": False,
}
MEAL = {
    "Id": "m1",
    "Title": "Lemon Soup",
    "MealDate": "2026-10-19T00:00:00-05:00",
    "MealTypeName": "Dinner",
    "Scale": "1",
    "Position": 0,
}


@pytest.fixture
def cache(tmp_path: Path) -> CloudCache:
    return CloudCache(tmp_path / "cache.db", CREDENTIALS.account_id)


def populate(cache: CloudCache, now: datetime | None = None) -> None:
    cache.apply(
        {"Recipe": {"r1": RECIPE}, "Meal": {"m1": MEAL}},
        {"Recipe": CURSOR, "Meal": CURSOR},
        now or datetime.now(UTC),
    )


def test_persistent_cache_serves_same_api(cache: CloudCache) -> None:
    populate(cache)
    reopened = CloudCache(cache.path, CREDENTIALS.account_id)
    database = CloudDatabase(reopened, 300)
    assert database.recipe("r1").ingredient_lines == ["1 lemon", "2 cups water"]
    plan = database.meal_plan(date(2026, 10, 19), date(2026, 10, 19))
    assert plan.items[0].date == date(2026, 10, 19)
    assert plan.items[0].recipe.id == "r1"
    assert database.shopping_preview(plan.start, plan.end).ready
    assert reopened.metadata()["cursors"]["Recipe"] == CURSOR
    with TestClient(create_app(database, TOKEN)) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        assert client.get("/v1/recipes?q=lemon").json()["total"] == 1
        status = client.get("/v1/status").json()
        assert status["backend"] == "cloud"
        assert status["cache_ready"] is True
        assert status["stale"] is False


def test_no_snapshot_is_unavailable_instead_of_empty(cache: CloudCache) -> None:
    database = CloudDatabase(cache, 300)
    assert database.status().cache_ready is False
    with pytest.raises(CacheUnavailable):
        database.search("", 25, 0)
    with TestClient(create_app(database, TOKEN)) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        assert client.get("/v1/recipes").status_code == 503
        assert client.get("/v1/status").json()["cache_ready"] is False


def test_successful_empty_account_is_available(cache: CloudCache) -> None:
    cache.apply(
        {"Recipe": {}, "Meal": {}},
        {"Recipe": CURSOR, "Meal": CURSOR},
        datetime.now(UTC),
    )
    database = CloudDatabase(cache, 300)
    assert database.status().cache_ready is True
    assert database.search("", 25, 0).total == 0


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ProbeError("network failed"), id="network"),
        pytest.param(AuthenticationError("credentials rejected"), id="authentication"),
        pytest.param(CloudHTTPError(503), id="service"),
    ],
)
def test_failures_preserve_cache_and_cursors(
    cache: CloudCache, failure: Exception
) -> None:
    populate(cache)
    old = cache.metadata()["last_successful_sync_at"]
    session = Mock()
    session.page.side_effect = failure
    worker = SyncWorker(cache, session)
    assert worker.run_once() >= 300
    metadata = cache.metadata()
    assert metadata["cursors"] == {"Recipe": CURSOR, "Meal": CURSOR}
    assert metadata["last_successful_sync_at"] == old
    assert metadata["last_sync_error"] is not None
    database = CloudDatabase(cache, 300)
    assert database.recipe("r1").title == "Lemon Soup"
    assert database.status().stale is True
    with TestClient(create_app(database, TOKEN)) as client:
        response = client.get(
            "/v1/recipes", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        assert response.headers["X-Recipe-Assistant-Stale"] == "true"
        assert "X-Recipe-Assistant-Last-Sync" in response.headers


def test_partial_pages_do_not_advance_cache(cache: CloudCache) -> None:
    populate(cache)
    session = Mock()
    session.page.side_effect = [
        {
            "Recipe": Page([{**RECIPE, "Title": "New title"}], NEXT_CURSOR, True),
            "Meal": Page([], CURSOR, False),
        },
        ProbeError("second page unavailable"),
    ]
    SyncWorker(cache, session).run_once()
    assert CloudDatabase(cache, 300).recipe("r1").title == "Lemon Soup"
    assert cache.metadata()["cursors"]["Recipe"] == CURSOR


def test_invalid_meal_rolls_back_recipe_and_cursor_changes(cache: CloudCache) -> None:
    populate(cache)
    with pytest.raises(ProbeError, match="meal date"):
        cache.apply(
            {
                "Recipe": {"r1": {**RECIPE, "Title": "Changed"}},
                "Meal": {"m1": {**MEAL, "MealDate": "bad"}},
            },
            {"Recipe": NEXT_CURSOR, "Meal": NEXT_CURSOR},
            datetime.now(UTC),
        )
    assert CloudDatabase(cache, 300).recipe("r1").title == "Lemon Soup"
    assert cache.metadata()["cursors"]["Recipe"] == CURSOR


def test_incremental_updates_and_tombstones(cache: CloudCache) -> None:
    populate(cache)
    session = Mock()
    session.page.return_value = {
        "Recipe": Page(
            [{**RECIPE, "Ingredients": "3 lemons", "Categories": ["Lunch"]}],
            NEXT_CURSOR,
            False,
        ),
        "Meal": Page([{"Id": "m1", "IsDeleted": True}], NEXT_CURSOR, False),
    }
    worker = SyncWorker(cache, session)
    worker.run_once()
    session.page.assert_called_once_with({"Recipe": CURSOR, "Meal": CURSOR})
    database = CloudDatabase(cache, 300)
    assert database.recipe("r1").ingredients == "3 lemons"
    assert database.recipe("r1").categories == ["Lunch"]
    assert database.status().meals == 0
    assert cache.metadata()["cursors"]["Recipe"] == NEXT_CURSOR
    session.page.return_value = {
        "Recipe": Page([{"Id": "r1", "IsDeleted": True}], NEXT_CURSOR, False),
        "Meal": Page([], NEXT_CURSOR, False),
    }
    worker.run_once()
    assert database.recipe("r1") is None
    with cache.connect() as conn:
        assert conn.execute("SELECT count(*) FROM RecipeCategories").fetchone()[0] == 0


def test_pagination_commits_sections_together(cache: CloudCache) -> None:
    session = Mock()
    session.page.side_effect = [
        {"Recipe": Page([RECIPE], CURSOR, True), "Meal": Page([MEAL], CURSOR, False)},
        {"Recipe": Page([], NEXT_CURSOR, False)},
    ]
    SyncWorker(cache, session).run_once()
    assert session.page.call_args_list[1].args[0] == {"Recipe": CURSOR}
    assert cache.metadata()["cursors"] == {"Recipe": NEXT_CURSOR, "Meal": CURSOR}
    assert CloudDatabase(cache, 300).status().recipes == 1


def test_401_renews_once_with_same_cursors(monkeypatch: pytest.MonkeyPatch) -> None:
    login = Mock(return_value=Authentication("new-private-token", 0, None, None, False))
    monkeypatch.setattr(cloud_backend, "fresh_login", login)
    session = CloudSession(CREDENTIALS)
    session._token = "old-private-token"
    session._send = Mock(
        side_effect=[
            CloudHTTPError(401),
            {"RecipeSyncEntityResponse": response_section([])},
        ]
    )
    result = session.page({"Recipe": CURSOR})
    assert result["Recipe"].cursor == CURSOR
    login.assert_called_once_with(CREDENTIALS.email, CREDENTIALS.password)
    assert session._send.call_args_list[0].args == session._send.call_args_list[1].args
    assert session._token == "new-private-token"


def test_repeated_401_stops_after_one_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    login = Mock(return_value=Authentication("renewed", None, None, None, False))
    monkeypatch.setattr(cloud_backend, "fresh_login", login)
    session = CloudSession(CREDENTIALS)
    session._token = "old"
    session._send = Mock(side_effect=CloudHTTPError(401))
    with pytest.raises(AuthenticationError):
        session.page({"Recipe": CURSOR})
    assert login.call_count == 1
    assert session._send.call_count == 2
    assert session._token == ""


def test_503_does_not_trigger_login(monkeypatch: pytest.MonkeyPatch) -> None:
    login = Mock()
    monkeypatch.setattr(cloud_backend, "fresh_login", login)
    session = CloudSession(CREDENTIALS)
    session._token = "existing"
    session._send = Mock(side_effect=CloudHTTPError(503))
    with pytest.raises(CloudHTTPError):
        session.page({"Recipe": CURSOR})
    login.assert_not_called()


def test_initial_session_logs_in_once(monkeypatch: pytest.MonkeyPatch) -> None:
    login = Mock(return_value=Authentication("fresh", 0, None, None, False))
    monkeypatch.setattr(cloud_backend, "fresh_login", login)
    session = CloudSession(CREDENTIALS)
    session._send = Mock(
        return_value={"RecipeSyncEntityResponse": response_section([])}
    )
    session.page({"Recipe": INITIAL_CURSOR})
    session.page({"Recipe": CURSOR})
    assert login.call_count == 1


def test_cache_account_isolation(cache: CloudCache) -> None:
    populate(cache)
    before = hashlib.sha256(cache.path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="different account"):
        CloudCache(
            cache.path, Credentials("other@example.invalid", "password").account_id
        )
    assert hashlib.sha256(cache.path.read_bytes()).hexdigest() == before


def test_foreign_database_is_never_changed(database: Database) -> None:
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    with pytest.raises(SchemaError, match="not owned"):
        CloudCache(database.path, CREDENTIALS.account_id)
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before


def test_cache_private_permissions(cache: CloudCache) -> None:
    assert cache.path.stat().st_mode & 0o777 == 0o600
    cache.path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        CloudCache(cache.path, CREDENTIALS.account_id)


def test_sync_lock_prevents_concurrent_refresh(cache: CloudCache) -> None:
    with cache.sync_lock():
        with pytest.raises(SyncBusy), cache.sync_lock():
            pytest.fail("Second process should not acquire the cache sync lock")


def test_backoff_recovery_and_retry_after(cache: CloudCache) -> None:
    session = Mock()
    session.page.side_effect = [
        CloudHTTPError(429, 3600),
        ProbeError("offline"),
        {"Recipe": Page([], CURSOR, False), "Meal": Page([], CURSOR, False)},
    ]
    worker = SyncWorker(cache, session)
    assert worker.run_once() == 3600
    assert worker.run_once() == 1200
    assert worker.run_once() == 300
    assert worker.failures == 0
    assert cache.metadata()["last_sync_error"] is None


def test_cache_age_marks_stale_without_error(cache: CloudCache) -> None:
    populate(cache, datetime.now(UTC) - timedelta(hours=1))
    state = CloudDatabase(cache, 300).status()
    assert state.stale is True
    assert state.cloud_sync_verified is False


def test_service_lifecycle_starts_and_stops_worker(cache: CloudCache) -> None:
    worker = Mock()
    worker.start.side_effect = lambda: populate(cache)
    with TestClient(create_app(CloudDatabase(cache, 300), TOKEN, worker)) as client:
        assert (
            client.get(
                "/v1/status", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 200
        )
    worker.start.assert_called_once()
    worker.stop.assert_called_once()


def test_credentials_file_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RECIPE_KEEPER_EMAIL", raising=False)
    monkeypatch.delenv("RECIPE_KEEPER_PASSWORD", raising=False)
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps({"email": CREDENTIALS.email, "password": CREDENTIALS.password})
    )
    path.chmod(0o600)
    assert load_credentials(path) == CREDENTIALS
    assert CREDENTIALS.password not in repr(load_credentials(path))
    monkeypatch.setenv("RECIPE_KEEPER_EMAIL", "environment@example.invalid")
    monkeypatch.setenv("RECIPE_KEEPER_PASSWORD", "environment-password")
    assert load_credentials(path).email == "environment@example.invalid"


def test_credentials_file_rejects_public_permissions(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_credentials(path)
