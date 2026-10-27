"""Public bridge response models."""

import datetime as dt
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class RecipeSummary(BaseModel):
    id: str
    title: str
    serving_size: str
    favourite: bool


class Recipe(RecipeSummary):
    source: str
    preparation_time_raw: int | None
    cooking_time_raw: int | None
    ingredients: str
    ingredient_lines: list[str]
    directions: str
    notes: str
    categories: list[str]
    linked_recipe_ids: list[str]


class RecipePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[RecipeSummary]


class Meal(BaseModel):
    id: str
    date: date
    title: str
    meal_type: str
    scale: str | None
    notes: str
    match: str
    candidate_recipe_ids: list[str]
    recipe: Recipe | None


class MealPlan(BaseModel):
    start: date
    end: date
    items: list[Meal]


class ShoppingLine(BaseModel):
    key: str
    meal_id: str
    recipe_id: str
    date: date
    recipe_title: str
    text: str
    selected: bool = True
    pantry_match: str | None = None


class ShoppingIssue(BaseModel):
    meal_id: str
    reason: str


class ShoppingPreview(BaseModel):
    start: date
    end: date
    ready: bool
    items: list[ShoppingLine]
    issues: list[ShoppingIssue]

    @computed_field
    @property
    def selected_items(self) -> list[ShoppingLine]:
        return [item for item in self.items if item.selected]


class ShoppingSelectionRequest(BaseModel):
    start: date
    end: date
    selected_keys: list[str] | None = Field(default=None, max_length=10000)


class Status(BaseModel):
    status: str = "ok"
    recipes: int
    meals: int
    categories: int = 0
    database_modified_at: datetime
    cloud_sync_verified: bool = False
    backend: str = "database"
    cache_ready: bool = True
    stale: bool = False
    last_sync_attempt_at: datetime | None = None
    last_successful_sync_at: datetime | None = None
    last_sync_error: str | None = None
    next_sync_at: datetime | None = None
    allow_edits: bool = False
    edit_operations: dict[str, int] = Field(default_factory=dict)


class EditFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def nonempty_changes(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Specify at least one non-null edit field")
        for key in ("title", "meal_type"):
            if key in self.model_fields_set and not getattr(self, key).strip():
                raise ValueError(f"{key} must contain text")
        if "scale" in self.model_fields_set and float(self.scale) <= 0:
            raise ValueError("Scale must be positive")
        return self


class RecipeChanges(EditFields):
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    serving_size: str | None = Field(default=None, max_length=1000)
    source: str | None = Field(default=None, max_length=10000)
    ingredients: str | None = Field(default=None, max_length=100000)
    directions: str | None = Field(default=None, max_length=100000)
    notes: str | None = Field(default=None, max_length=100000)
    favourite: bool | None = None
    categories: list[str] | None = Field(default=None, max_length=500)
    rating: int | None = Field(default=None, ge=0, le=5)
    preparation_time_raw: int | None = Field(default=None, ge=0)
    cooking_time_raw: int | None = Field(default=None, ge=0)


class NewRecipe(RecipeChanges):
    title: str = Field(min_length=1, max_length=1000)


class MealChanges(EditFields):
    title: str | None = Field(default=None, min_length=1, max_length=1000)
    date: dt.date | None = None
    meal_type: str | None = Field(default=None, min_length=1, max_length=100)
    meal_type_code: int | None = Field(default=None, ge=0, le=10000)
    scale: str | None = Field(
        default=None, pattern=r"^\d{1,4}(?:\.\d{1,4})?$", max_length=9
    )
    notes: str | None = Field(default=None, max_length=100000)
    position: int | None = Field(default=None, ge=0, le=100000)
    meal_time_raw: int | None = Field(default=None, ge=0)
    colour_raw: int | None = None


class NewMeal(MealChanges):
    title: str = Field(min_length=1, max_length=1000)
    date: dt.date


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID


class RecipeCreateRequest(EditRequest):
    changes: NewRecipe


class MealCreateRequest(EditRequest):
    changes: NewMeal


class DeleteRequest(EditRequest):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecipeUpdateRequest(DeleteRequest):
    changes: RecipeChanges


class MealUpdateRequest(DeleteRequest):
    changes: MealChanges


class SyncTombstone(BaseModel):
    id: str
    revision: str
    last_modified: str


class SyncRecipe(Recipe):
    revision: str
    last_modified: str
    rating: int | None


class SyncMeal(Meal):
    revision: str
    last_modified: str
    meal_type_code: int | None
    position: int | None
    meal_time_raw: int | None
    colour_raw: int | None


class SyncOperation(BaseModel):
    operation_id: str
    entity_type: str
    entity_id: str
    action: str
    state: str
    error: str | None
    created_at: str


class SyncSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    scope: Literal["all_cached_recipes_and_meals"] = "all_cached_recipes_and_meals"
    account_id: str
    snapshot_id: str
    complete: Literal[True] = True
    stale: bool
    allow_edits: bool
    last_successful_sync_at: datetime
    recipes: list[SyncRecipe]
    meals: list[SyncMeal]
    deleted_recipes: list[SyncTombstone]
    deleted_meals: list[SyncTombstone]
    unresolved_operations: list[SyncOperation]
