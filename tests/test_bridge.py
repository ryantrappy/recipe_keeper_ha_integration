import hashlib
import os
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recipe_assistant.api import create_app
from recipe_assistant.database import Database, SchemaError, ticks

from .conftest import TOKEN


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic abc"])
def test_auth(client: TestClient, authorization: str | None) -> None:
    client.headers.pop("Authorization")
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.get("/v1/status", headers=headers)
    assert response.status_code == 401


def test_read_only_and_no_mutation(database: Database, client: TestClient) -> None:
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    with database.connection() as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM Recipes")
    assert client.get("/v1/recipes?q=lemon").json()["total"] == 1
    recipe = client.get("/v1/recipes/r1").json()
    assert recipe["ingredient_lines"] == ["1 lemon", "2 cups water"]
    assert recipe["directions"] == "Simmer."
    assert recipe["categories"] == ["Dinner"]
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before
    assert client.post("/v1/recipes").status_code == 403


@pytest.mark.parametrize("query", ["CRÈME", "beans cup", "' OR 1=1 --", "%", "_"])
def test_literal_search(database: Database, query: str) -> None:
    expected = 1 if query in ("CRÈME", "beans cup") else 0
    assert database.search(query, 25, 0).total == expected


def test_paging_and_deleted(client: TestClient) -> None:
    page = client.get("/v1/recipes?limit=1&offset=1").json()
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert client.get("/v1/recipes/deleted").status_code == 404
    assert client.get("/v1/recipes?limit=0").status_code == 422


def test_plan_and_preview(client: TestClient) -> None:
    query = "?start=2026-10-19&end=2026-10-19"
    response = client.get("/v1/meal-plan" + query)
    assert response.status_code == 200
    meal = response.json()["items"][0]
    assert meal["date"] == "2026-10-19"
    assert meal["recipe"]["id"] == "r1"
    preview = client.get("/v1/shopping-preview" + query).json()
    assert preview["ready"] is True
    assert [item["text"] for item in preview["items"]] == ["1 lemon", "2 cups water"]
    assert preview == client.get("/v1/shopping-preview" + query).json()
    assert (
        client.get("/v1/meal-plan?start=2026-10-20&end=2026-10-20").json()["items"]
        == []
    )


@pytest.mark.parametrize("scale", ["2", "1/2", "", None, "NaN"])
def test_non_unit_scale_is_not_silently_used(
    database: Database,
    scale: str | None,
) -> None:
    with sqlite3.connect(database.path) as conn:
        conn.execute("UPDATE Meals SET Scale=?", (scale,))
    preview = database.shopping_preview(date(2026, 10, 19), date(2026, 10, 19))
    assert preview.ready is False
    assert preview.items == []
    assert "scale" in preview.issues[0].reason


@pytest.mark.parametrize(
    "title,match", [("Unknown", "unmatched"), ("Duplicate", "ambiguous")]
)
def test_unresolved_title(database: Database, title: str, match: str) -> None:
    with sqlite3.connect(database.path) as conn:
        conn.execute("UPDATE Meals SET Title=?", (title,))
        conn.executemany(
            "INSERT INTO Recipes (Id, Title) VALUES (?, ?)",
            [("dup1", "Duplicate"), ("dup2", "Duplicate")],
        )
    plan = database.meal_plan(date(2026, 10, 19), date(2026, 10, 19))
    assert plan.items[0].match == match
    assert plan.items[0].recipe is None
    assert not database.shopping_preview(plan.start, plan.end).ready


@pytest.mark.parametrize(
    "start,end",
    [
        ("bad", "2026-10-19"),
        ("2026-10-20", "2026-10-19"),
        ("2025-01-01", "2026-10-19"),
        ("9999-12-31", "9999-12-31"),
    ],
)
def test_bad_date_range(client: TestClient, start: str, end: str) -> None:
    assert client.get(f"/v1/meal-plan?start={start}&end={end}").status_code == 422


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with TestClient(create_app(Database(path), TOKEN)) as client:
        response = client.get(
            "/v1/status", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 503
    assert not path.exists()


def test_schema_drift_fails(database: Database) -> None:
    with sqlite3.connect(database.path) as conn:
        conn.execute("ALTER TABLE Meals RENAME COLUMN Scale TO ChangedScale")
    with pytest.raises(SchemaError, match="Scale"):
        database.status()


def test_wal_and_fresh_reads(database: Database) -> None:
    with sqlite3.connect(database.path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE Recipes SET Ingredients='3 lemons' WHERE Id='r1'")
        writer.commit()
        assert database.recipe("r1").ingredient_lines == ["3 lemons"]
        writer.execute("UPDATE Recipes SET Ingredients='4 lemons' WHERE Id='r1'")
        writer.commit()
        assert database.recipe("r1").ingredient_lines == ["4 lemons"]


def test_date_ticks() -> None:
    assert ticks(date(2026, 10, 20)) == 639280512000000000


@pytest.mark.parametrize(
    "ingredients,linked,reason",
    [
        pytest.param("Sauce:\n1 lemon", False, "headings", id="heading"),
        pytest.param("# Sauce\n1 lemon", False, "headings", id="hash-heading"),
        pytest.param("", False, "no ingredients", id="empty"),
        pytest.param("1 lemon", True, "linked", id="linked"),
    ],
)
def test_preview_flags_incomplete_ingredients(
    database: Database,
    ingredients: str,
    linked: bool,
    reason: str,
) -> None:
    with sqlite3.connect(database.path) as conn:
        conn.execute("UPDATE Recipes SET Ingredients=? WHERE Id='r1'", (ingredients,))
        conn.execute(
            "INSERT INTO LinkedRecipes (RecipeId, LinkedRecipeId) "
            "SELECT 'r1', 'r2' WHERE ?",
            (linked,),
        )
    result = database.shopping_preview(date(2026, 10, 19), date(2026, 10, 19))
    assert result.ready is False
    assert result.items == []
    assert reason in result.issues[0].reason


def test_deleted_meals_and_inclusive_end(database: Database) -> None:
    with sqlite3.connect(database.path) as conn:
        conn.execute(
            "INSERT INTO Meals (Id, MealDate, Title, IsDeleted) "
            "VALUES ('deleted', ?, 'Lemon Soup', 1)",
            (ticks(date(2026, 10, 19)),),
        )
        conn.execute(
            "INSERT INTO Meals (Id, MealDate, Title, Scale) VALUES ('end', ?, ?, '1')",
            (ticks(date(2026, 10, 20)) + 863_990_000_000, "Crème Beans"),
        )
    result = database.meal_plan(date(2026, 10, 19), date(2026, 10, 20))
    assert [m.id for m in result.items] == ["m1", "end"]


def test_snapshot_does_not_mix_commits(database: Database) -> None:
    with sqlite3.connect(database.path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        with database.connection() as reader:
            assert (
                reader.execute("SELECT Title FROM Recipes WHERE Id='r1'").fetchone()[0]
                == "Lemon Soup"
            )
            writer.execute("UPDATE Recipes SET Title='Changed' WHERE Id='r1'")
            writer.commit()
            assert (
                reader.execute("SELECT Title FROM Recipes WHERE Id='r1'").fetchone()[0]
                == "Lemon Soup"
            )
        assert database.recipe("r1").title == "Changed"


def test_init_preserves_existing_config(tmp_path: Path) -> None:
    config = tmp_path / ".env"
    result = subprocess.run(
        ["recipe-assistant", "init", "--env-file", str(config)],
        capture_output=True,
        text=True,
        check=True,
    )
    original = config.read_bytes()
    assert os.stat(config).st_mode & 0o777 == 0o600
    assert b"RECIPE_ASSISTANT_TOKEN=" in original
    assert original.splitlines()[0].split(b"=", 1)[1].decode() not in result.stdout
    second = subprocess.run(
        ["recipe-assistant", "init", "--env-file", str(config)],
        capture_output=True,
        text=True,
    )
    assert second.returncode != 0
    assert config.read_bytes() == original


def test_unavailable_response_does_not_leak_path(database: Database) -> None:
    with sqlite3.connect(database.path) as conn:
        conn.execute("DROP TABLE Meals")
    with TestClient(create_app(database, TOKEN)) as client:
        response = client.get(
            "/v1/status", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 503
    assert str(database.path) not in response.text


@pytest.mark.parametrize("token", ["", "short", "replace-with-a-long-random-token"])
def test_weak_token_rejected(database: Database, token: str) -> None:
    with pytest.raises(ValueError, match="TOKEN"):
        create_app(database, token)
