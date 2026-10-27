"""Read Recipe Keeper's SQLite data without altering it."""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import (
    Meal,
    MealPlan,
    Recipe,
    RecipePage,
    RecipeSummary,
    ShoppingIssue,
    ShoppingLine,
    ShoppingPreview,
    Status,
)

DEFAULT_DATABASE = (
    Path.home()
    / "Library/Containers/com.tudorspan.recipekeeper-macos"
    / "Data/Documents/RecipeKeeper.db"
)
EPOCH = datetime(1, 1, 1)
REQUIRED_COLUMNS = {
    "Recipes": {
        "Id",
        "IsDeleted",
        "Title",
        "ServingSize",
        "IsFavourite",
        "Source",
        "PreparationTime",
        "CookingTime",
        "Ingredients",
        "Directions",
        "Notes",
    },
    "Meals": {
        "Id",
        "IsDeleted",
        "MealDate",
        "Title",
        "MealTypeName",
        "Scale",
        "Notes",
        "Position",
        "MealTime",
    },
    "RecipeCategories": {"RecipeId", "Name"},
    "LinkedRecipes": {"Id", "RecipeId", "LinkedRecipeId"},
}


class SchemaError(Exception):
    """The database does not have the supported Recipe Keeper schema."""


def ticks(value: date) -> int:
    """Return .NET ticks for a local calendar date, without a timezone shift."""
    return (datetime.combine(value, time.min) - EPOCH).days * 864_000_000_000


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # mode=ro must not use immutable=1: live databases may have a WAL.
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            for table, required in REQUIRED_COLUMNS.items():
                columns = {
                    r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')
                }
                if missing := required - columns:
                    raise SchemaError(f"{table}: missing {', '.join(sorted(missing))}")
            yield conn
        finally:
            conn.close()

    def status(self) -> Status:
        with self.connection() as conn:
            counts = {
                table: conn.execute(
                    f"SELECT count(*) FROM {table} WHERE IsDeleted=0"
                ).fetchone()[0]
                for table in ("Recipes", "Meals")
            }
            categories = conn.execute(
                "SELECT count(DISTINCT c.Name) FROM RecipeCategories c "
                "JOIN Recipes r ON r.Id=c.RecipeId WHERE r.IsDeleted=0 "
                "AND c.Name IS NOT NULL AND trim(c.Name)!=''"
            ).fetchone()[0]
            modified = max(
                p.stat().st_mtime
                for p in (self.path, Path(str(self.path) + "-wal"))
                if p.exists()
            )
        return Status(
            recipes=counts["Recipes"],
            meals=counts["Meals"],
            categories=categories,
            database_modified_at=datetime.fromtimestamp(modified, UTC),
        )

    @staticmethod
    def summary(row: sqlite3.Row) -> RecipeSummary:
        return RecipeSummary(
            id=row["Id"],
            title=row["Title"] or "",
            serving_size=row["ServingSize"] or "",
            favourite=bool(row["IsFavourite"]),
        )

    def search(self, query: str, limit: int, offset: int) -> RecipePage:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT Id, Title, ServingSize, IsFavourite, Ingredients "
                "FROM Recipes WHERE IsDeleted=0 ORDER BY Title COLLATE NOCASE, Id"
            ).fetchall()
            terms = query.casefold().split()
            rows = [
                row
                for row in rows
                if all(
                    term
                    in (
                        (row["Title"] or "") + "\n" + (row["Ingredients"] or "")
                    ).casefold()
                    for term in terms
                )
            ]
            return RecipePage(
                total=len(rows),
                limit=limit,
                offset=offset,
                items=[self.summary(r) for r in rows[offset : offset + limit]],
            )

    def recipe(self, recipe_id: str) -> Recipe | None:
        with self.connection() as conn:
            return self._recipe(conn, recipe_id)

    def _recipe(self, conn: sqlite3.Connection, recipe_id: str) -> Recipe | None:
        row = conn.execute(
            "SELECT Id, Title, ServingSize, IsFavourite, Source, PreparationTime, "
            "CookingTime, Ingredients, Directions, Notes FROM Recipes "
            "WHERE Id=? AND IsDeleted=0",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return None
        ingredients = row["Ingredients"] or ""
        return Recipe(
            **self.summary(row).model_dump(),
            source=row["Source"] or "",
            preparation_time_raw=row["PreparationTime"],
            cooking_time_raw=row["CookingTime"],
            ingredients=ingredients,
            ingredient_lines=[
                line.strip() for line in ingredients.splitlines() if line.strip()
            ],
            directions=row["Directions"] or "",
            notes=row["Notes"] or "",
            categories=[
                r[0]
                for r in conn.execute(
                    "SELECT Name FROM RecipeCategories WHERE RecipeId=? ORDER BY Name",
                    (recipe_id,),
                )
            ],
            linked_recipe_ids=[
                str(r[0])
                for r in conn.execute(
                    "SELECT LinkedRecipeId FROM LinkedRecipes "
                    "WHERE RecipeId=? ORDER BY Id",
                    (recipe_id,),
                )
            ],
        )

    def meal_plan(self, start: date, end: date) -> MealPlan:
        with self.connection() as conn:
            titles: dict[str, list[str]] = {}
            for r in conn.execute("SELECT Id, Title FROM Recipes WHERE IsDeleted=0"):
                titles.setdefault((r["Title"] or "").casefold(), []).append(r["Id"])
            rows = conn.execute(
                "SELECT * FROM Meals WHERE IsDeleted=0 AND MealDate>=? "
                "AND MealDate<? ORDER BY MealDate, MealTime, Position, Id",
                (ticks(start), ticks(end + timedelta(days=1))),
            )
            meals = []
            for row in rows:
                candidates = sorted(titles.get((row["Title"] or "").casefold(), []))
                match = (
                    "matched"
                    if len(candidates) == 1
                    else ("ambiguous" if candidates else "unmatched")
                )
                meals.append(
                    Meal(
                        id=row["Id"],
                        date=(
                            EPOCH + timedelta(microseconds=row["MealDate"] // 10)
                        ).date(),
                        title=row["Title"] or "",
                        meal_type=row["MealTypeName"] or "",
                        scale=row["Scale"],
                        notes=row["Notes"] or "",
                        match=match,
                        candidate_recipe_ids=candidates,
                        recipe=self._recipe(conn, candidates[0])
                        if match == "matched"
                        else None,
                    )
                )
        return MealPlan(start=start, end=end, items=meals)

    def calendar_meals(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT Id, MealDate, Title, MealTypeName, Notes FROM Meals "
                "WHERE IsDeleted=0 ORDER BY MealDate, MealTime, Position, Id"
            ).fetchall()
        return [
            {
                "id": row["Id"],
                "date": (EPOCH + timedelta(microseconds=row["MealDate"] // 10))
                .date()
                .isoformat(),
                "title": row["Title"] or "",
                "meal_type": row["MealTypeName"] or "",
                "notes": row["Notes"] or "",
            }
            for row in rows
        ]

    def shopping_preview(self, start: date, end: date) -> ShoppingPreview:
        items = []
        issues = []
        for meal in self.meal_plan(start, end).items:
            reasons = []
            if meal.recipe is None:
                reasons.append(f"{meal.match} recipe title")
            try:
                scale_is_one = Decimal(meal.scale or "") == 1
            except InvalidOperation:
                scale_is_one = False
            if not scale_is_one:
                reasons.append("serving scale needs review; quantities were not scaled")
            if meal.recipe is not None:
                if meal.recipe.linked_recipe_ids:
                    reasons.append("linked recipes need review")
                if not meal.recipe.ingredient_lines:
                    reasons.append("recipe has no ingredients")
                if any(
                    line.startswith("#") or line.endswith(":")
                    for line in meal.recipe.ingredient_lines
                ):
                    reasons.append("ingredient section headings need review")
            if reasons:
                issues.extend(ShoppingIssue(meal_id=meal.id, reason=r) for r in reasons)
                continue
            assert meal.recipe is not None
            for index, line in enumerate(meal.recipe.ingredient_lines):
                identity = f"{meal.id}\0{meal.recipe.id}\0{index}\0{line}"
                items.append(
                    ShoppingLine(
                        key=hashlib.sha256(identity.encode()).hexdigest(),
                        meal_id=meal.id,
                        recipe_id=meal.recipe.id,
                        date=meal.date,
                        recipe_title=meal.recipe.title,
                        text=line,
                    )
                )
        return ShoppingPreview(
            start=start,
            end=end,
            ready=not issues,
            items=items,
            issues=issues,
        )
