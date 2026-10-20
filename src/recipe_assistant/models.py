"""Public bridge response models."""

from datetime import date, datetime

from pydantic import BaseModel


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


class ShoppingIssue(BaseModel):
    meal_id: str
    reason: str


class ShoppingPreview(BaseModel):
    start: date
    end: date
    ready: bool
    items: list[ShoppingLine]
    issues: list[ShoppingIssue]


class Status(BaseModel):
    status: str = "ok"
    recipes: int
    meals: int
    database_modified_at: datetime
    cloud_sync_verified: bool = False
