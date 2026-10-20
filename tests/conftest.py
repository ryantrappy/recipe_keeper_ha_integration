"""Synthetic data only; never use the user's recipes in tests."""

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recipe_assistant.api import create_app
from recipe_assistant.database import Database, ticks

TOKEN = "test-token-" + "x" * 32


@pytest.fixture
def database(tmp_path: Path) -> Database:
    path = tmp_path / "RecipeKeeper.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE Recipes (
                Id TEXT PRIMARY KEY, IsDeleted INTEGER DEFAULT 0, Title TEXT,
                ServingSize TEXT, IsFavourite INTEGER DEFAULT 0, Source TEXT,
                PreparationTime INTEGER, CookingTime INTEGER, Ingredients TEXT,
                Directions TEXT, Notes TEXT
            );
            CREATE TABLE Meals (
                Id TEXT PRIMARY KEY, IsDeleted INTEGER DEFAULT 0, MealDate INTEGER,
                Title TEXT, MealTypeName TEXT, Scale TEXT, Notes TEXT,
                Position INTEGER, MealTime TEXT
            );
            CREATE TABLE RecipeCategories (RecipeId TEXT, Name TEXT);
            CREATE TABLE LinkedRecipes (Id INTEGER PRIMARY KEY, RecipeId TEXT,
                LinkedRecipeId TEXT);
            INSERT INTO Recipes (Id, Title, ServingSize, Ingredients, Directions)
                VALUES ('r1', 'Lemon Soup', '4', '1 lemon\n2 cups water', 'Simmer.');
            INSERT INTO Recipes (Id, Title, Ingredients)
                VALUES ('r2', 'Crème Beans', '1 cup beans');
            INSERT INTO Recipes (Id, Title, IsDeleted)
                VALUES ('deleted', 'Lemon Soup', 1);
            INSERT INTO RecipeCategories VALUES ('r1', 'Dinner');
        """)
        conn.execute(
            "INSERT INTO Meals (Id, MealDate, Title, MealTypeName, Scale) "
            "VALUES ('m1', ?, 'Lemon Soup', 'Dinner', '1')",
            (ticks(date(2026, 10, 19)),),
        )
    return Database(path)


@pytest.fixture
def client(database: Database) -> Iterator[TestClient]:
    with TestClient(
        create_app(database, TOKEN), headers={"Authorization": f"Bearer {TOKEN}"}
    ) as client:
        yield client
