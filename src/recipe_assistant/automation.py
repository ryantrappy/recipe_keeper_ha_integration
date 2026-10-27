"""Durable recipe import and meal planning for automation clients."""

import json
import secrets
from collections import Counter
from collections.abc import Callable

from .cloud_backend import CloudDatabase
from .cloud_edits import CloudEditor, EditConflict, EntityNotFound, revision
from .models import MealChanges, NewMeal, NewRecipe

MEAL_TYPES = ("breakfast", "lunch", "dinner", "side", "dessert", "drink", "snack")


def meal_category(label: str) -> str:
    value = label.strip().casefold()
    return value if value in MEAL_TYPES else "other"


class AutomationActions:
    def __init__(self, editor: CloudEditor, database: CloudDatabase) -> None:
        self.editor = editor
        self.database = database

    def prepared(self, operation_id: str, intent: dict) -> dict | None:
        self.editor._require_enabled()
        with self.editor.cache.connect() as conn:
            row = conn.execute(
                "SELECT Fingerprint, ResolvedEdit FROM AutomationRequests WHERE Id=?",
                (operation_id,),
            ).fetchone()
        if row:
            if row[0] != revision(intent):
                raise EditConflict("Operation ID was used for a different request")
            return json.loads(row[1])
        if self.editor.operation(operation_id):
            raise EditConflict("Operation ID belongs to another edit action")
        return None

    def _prepare(
        self, operation_id: str, intent: dict, resolve: Callable[[], dict]
    ) -> dict:
        with self.editor.cache.sync_lock():
            if previous := self.prepared(operation_id, intent):
                return previous
            self.editor.worker.sync_locked()
            self.editor._require_enabled()
            resolved = resolve()
            with self.editor.cache.connect() as conn:
                conn.execute(
                    "INSERT INTO AutomationRequests VALUES (?, ?, ?)",
                    (operation_id, revision(intent), json.dumps(resolved)),
                )
            return resolved

    def execute(self, operation_id: str, resolved: dict) -> dict:
        def validate_recipe_link() -> None:
            recipe_id = resolved.get("recipe_id")
            if not recipe_id:
                return
            recipe = self.database.recipe(recipe_id)
            title = resolved["changes"]["title"]
            matches = [
                row
                for row in self.database.search("", 2**31 - 1, 0).items
                if row.title.casefold() == title.casefold()
            ]
            if recipe is None or recipe.title != title or len(matches) != 1:
                raise EditConflict(
                    "Selected recipe title changed or became ambiguous; "
                    "no meal was uploaded. Review a new planning request"
                )

        result = self.editor.edit(
            resolved["section"],
            resolved["action"],
            operation_id,
            resolved["changes"],
            entity_id=resolved.get("entity_id"),
            expected_revision=resolved.get("expected_revision"),
            preflight=validate_recipe_link,
        )
        if resolved["section"] == "Meal":
            result["mealplan_id"] = result["entity_id"]
        if resolved.get("recipe_id"):
            result["recipe_id"] = resolved["recipe_id"]
        if result["state"] == "confirmed":
            if resolved["section"] == "Recipe":
                recipe = self.database.recipe(result["entity_id"])
                result["recipe"] = recipe.model_dump(mode="json") if recipe else None
            else:
                planned_date = NewMeal.model_validate(resolved["changes"]).date
                meal = next(
                    (
                        item
                        for item in self.database.meal_plan(
                            planned_date, planned_date
                        ).items
                        if item.id == result["entity_id"]
                    ),
                    None,
                )
                result["mealplan"] = (
                    {
                        **meal.model_dump(mode="json"),
                        "revision": self.editor.view("Meal", meal.id)["revision"],
                    }
                    if meal
                    else None
                )
        return result

    def plan(self, operation_id: str, intent: dict) -> dict:
        def resolve() -> dict:
            if intent["entry_type"] not in MEAL_TYPES:
                raise ValueError("Unsupported meal type")
            recipe_id = intent.get("recipe_id")
            all_recipes = self.database.search("", 2**31 - 1, 0).items
            titles = Counter(recipe.title.casefold() for recipe in all_recipes)
            if intent["action"] == "set_random_mealplan":
                candidates = self.database.search(
                    intent["search_terms"], 2**31 - 1, 0
                ).items
                candidates = [
                    recipe
                    for recipe in candidates
                    if recipe.title.strip()
                    and titles[recipe.title.casefold()] == 1
                    and (not intent["favourite_only"] or recipe.favourite)
                ]
                if not candidates:
                    raise ValueError(
                        "No uniquely titled recipes match the random selection"
                    )
                recipe_id = secrets.choice(candidates).id
            changes = {
                "date": intent["date"],
                "meal_type": intent["entry_type"].capitalize(),
            }
            if recipe_id:
                recipe = self.database.recipe(recipe_id)
                if recipe is None:
                    raise EntityNotFound("Recipe not found")
                if not recipe.title.strip() or titles[recipe.title.casefold()] != 1:
                    raise ValueError(
                        "Recipe title is empty or ambiguous; "
                        "rename it before planning by ID"
                    )
                changes["title"] = recipe.title
            else:
                changes["title"] = intent["note_title"]
            if intent.get("note_text") is not None:
                changes["notes"] = intent["note_text"]
            update = intent["action"] == "update_mealplan"
            validated = (MealChanges if update else NewMeal).model_validate(changes)
            return {
                "section": "Meal",
                "action": "update" if update else "create",
                "changes": validated.model_dump(mode="json", exclude_unset=True),
                "entity_id": intent.get("mealplan_id"),
                "expected_revision": intent.get("revision"),
                "recipe_id": recipe_id,
            }

        return self.execute(operation_id, self._prepare(operation_id, intent, resolve))

    def import_recipe(self, operation_id: str, intent: dict, changes: dict) -> dict:
        def resolve() -> dict:
            return {
                "section": "Recipe",
                "action": "create",
                "changes": NewRecipe.model_validate(changes).model_dump(
                    mode="json", exclude_unset=True
                ),
            }

        return self.execute(operation_id, self._prepare(operation_id, intent, resolve))
