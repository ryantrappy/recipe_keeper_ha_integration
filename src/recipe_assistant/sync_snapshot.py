"""Export one consistent cloud cache snapshot for external meal synchronizers."""

import json
from datetime import UTC, datetime, timedelta

from .cloud_backend import CacheUnavailable, CloudDatabase
from .cloud_edits import CloudEditor, revision
from .cloud_protocol import ProbeError
from .database import EPOCH
from .models import (
    Recipe,
    SyncMeal,
    SyncOperation,
    SyncRecipe,
    SyncSnapshot,
    SyncTombstone,
)


def read_sync_snapshot(
    database: CloudDatabase, allow_edits: bool = False
) -> SyncSnapshot:
    recipes: dict[str, SyncRecipe] = {}
    meals: list[SyncMeal] = []
    deleted = {"Recipe": [], "Meal": []}
    identities = []
    with database.connection() as conn:
        metadata = {
            key: json.loads(value)
            for key, value in conn.execute("SELECT key, value FROM Metadata")
        }
        if metadata.get("raw_snapshot_ready") is not True:
            raise CacheUnavailable("A complete cloud refresh is required")
        raw_meals = []
        for section, entity_id, serialized in conn.execute(
            "SELECT Section, Id, Record FROM RawRecords ORDER BY Section, Id"
        ):
            record = json.loads(serialized)
            modified = record.get("LastModified")
            if (
                record["Id"] != entity_id
                or not isinstance(modified, str)
                or not modified
            ):
                raise ProbeError("Cloud record lacks synchronization metadata")
            entity_revision = revision(record)
            identities.append([section, entity_id, entity_revision])
            if record.get("IsDeleted"):
                deleted[section].append(
                    SyncTombstone(
                        id=entity_id,
                        revision=entity_revision,
                        last_modified=modified,
                    )
                )
            elif section == "Recipe":
                recipe = database._recipe(conn, entity_id)
                if recipe is None:
                    raise ProbeError("Cloud snapshot projections do not match")
                recipes[entity_id] = SyncRecipe(
                    **recipe.model_dump(),
                    revision=entity_revision,
                    last_modified=modified,
                    rating=record.get("Rating"),
                )
            else:
                raw_meals.append((record, entity_revision))
        titles: dict[str, list[str]] = {}
        for recipe in recipes.values():
            titles.setdefault(recipe.title.casefold(), []).append(recipe.id)
        rows = {row["Id"]: row for row in conn.execute("SELECT * FROM Meals")}
        for record, entity_revision in raw_meals:
            row = rows.get(record["Id"])
            if row is None:
                raise ProbeError("Cloud snapshot projections do not match")
            candidates = sorted(titles.get((row["Title"] or "").casefold(), []))
            match = (
                "matched"
                if len(candidates) == 1
                else "ambiguous"
                if candidates
                else "unmatched"
            )
            meals.append(
                SyncMeal(
                    id=row["Id"],
                    date=(EPOCH + timedelta(microseconds=row["MealDate"] // 10)).date(),
                    title=row["Title"] or "",
                    meal_type=row["MealTypeName"] or "",
                    scale=row["Scale"],
                    notes=row["Notes"] or "",
                    match=match,
                    candidate_recipe_ids=candidates,
                    recipe=Recipe.model_validate(recipes[candidates[0]].model_dump())
                    if match == "matched"
                    else None,
                    revision=entity_revision,
                    last_modified=record["LastModified"],
                    meal_type_code=record.get("MealType"),
                    position=record.get("Position"),
                    meal_time_raw=record.get("MealTime"),
                    colour_raw=record.get("Colour"),
                )
            )
        projected_recipes = {row[0] for row in conn.execute("SELECT Id FROM Recipes")}
        if projected_recipes != set(recipes) or set(rows) != {
            meal.id for meal in meals
        }:
            raise ProbeError("Cloud snapshot projections do not match")
        last_sync = datetime.fromisoformat(metadata["last_successful_sync_at"])
        stale = (
            bool(metadata.get("last_sync_error"))
            or (datetime.now(UTC) - last_sync).total_seconds()
            > database.poll_seconds * 2
        )
        unresolved = [
            SyncOperation.model_validate(CloudEditor._result(row))
            for row in conn.execute(
                "SELECT * FROM EditOperations WHERE State IN "
                "('prepared', 'sending', 'uncertain') ORDER BY Id"
            )
        ]
        return SyncSnapshot(
            account_id=metadata["account_id"],
            snapshot_id=revision(
                {
                    "account_id": metadata["account_id"],
                    "records": identities,
                    "unresolved_operations": [item.model_dump() for item in unresolved],
                }
            ),
            last_successful_sync_at=last_sync,
            stale=stale,
            allow_edits=allow_edits,
            recipes=list(recipes.values()),
            meals=meals,
            deleted_recipes=deleted["Recipe"],
            deleted_meals=deleted["Meal"],
            unresolved_operations=unresolved,
        )
