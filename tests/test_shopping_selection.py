"""Ingredient selection preserves recipe text and supports pantry overrides."""

import hashlib
from datetime import date

import pytest
from fastapi.testclient import TestClient

from recipe_assistant.api import create_app
from recipe_assistant.database import Database
from recipe_assistant.models import ShoppingLine, ShoppingPreview
from recipe_assistant.shopping import (
    SelectionChangedError,
    pantry_match,
    parse_staples,
    select_ingredients,
)

from .conftest import TOKEN


@pytest.fixture
def preview() -> ShoppingPreview:
    return ShoppingPreview(
        start=date(2026, 10, 19),
        end=date(2026, 10, 19),
        ready=True,
        issues=[],
        items=[
            ShoppingLine(
                key=key,
                meal_id="meal",
                recipe_id="recipe",
                date=date(2026, 10, 19),
                recipe_title="Soup",
                text=text,
            )
            for key, text in [
                ("oil", "2 tbsp extra-virgin olive oil"),
                ("beans", "1 can white beans"),
                ("water", "2 cups water"),
            ]
        ],
    )


@pytest.mark.parametrize(
    "text",
    [
        "2 tbsp extra-virgin olive oil",
        "½ cup OLIVE OIL",
        "olive oil, divided",
        "1/4 cup olive oil (plus more)",
        "1–2 tablespoons olive oil for frying",
    ],
)
def test_olive_oil_matching(text: str) -> None:
    assert pantry_match(text, "olive oil")


@pytest.mark.parametrize(
    "text",
    [
        "1 jar olive oil mayonnaise",
        "2 tbsp olive oil cooking spray",
        "mayonnaise made with olive oil",
        "1 cup sunflower oil",
        "1 tbsp olive oils",
        "1 tbsp olive oil or butter",
    ],
)
def test_staples_do_not_hide_distinct_or_uncertain_ingredients(text: str) -> None:
    assert not pantry_match(text, "olive oil")


def test_pantry_defaults_remain_visible_and_preserve_recipe(
    preview: ShoppingPreview,
) -> None:
    result = select_ingredients(preview, ["olive oil"])
    assert [item.key for item in result.selected_items] == ["beans", "water"]
    assert result.items[0].selected is False
    assert result.items[0].pantry_match == "olive oil"
    assert result.items[0].text == "2 tbsp extra-virgin olive oil"
    assert len(result.items) == 3
    assert preview.items[0].selected is True
    assert preview.items[0].pantry_match is None


def test_explicit_selection_overrides_defaults_without_changing_staples(
    preview: ShoppingPreview,
) -> None:
    result = select_ingredients(preview, ["olive oil"], ["oil", "beans"])
    assert [item.key for item in result.selected_items] == ["oil", "beans"]
    assert result.items[0].pantry_match == "olive oil"
    assert [
        item.key for item in select_ingredients(preview, ["olive oil"]).selected_items
    ] == ["beans", "water"]


def test_select_nothing(preview: ShoppingPreview) -> None:
    assert select_ingredients(preview, ["olive oil"], []).selected_items == []


def test_unknown_key_requires_new_preview(preview: ShoppingPreview) -> None:
    with pytest.raises(SelectionChangedError, match="refresh"):
        select_ingredients(preview, [], ["old-key"])


def test_selection_keeps_review_issues(preview: ShoppingPreview) -> None:
    preview.ready = False
    assert not select_ingredients(preview, ["olive oil"], ["beans"]).ready


@pytest.mark.parametrize("value", ["olive oil", '"olive oil"', '[""]', "[42]", "{}"])
def test_invalid_staple_configuration(value: str) -> None:
    with pytest.raises(ValueError):
        parse_staples(value)


def test_staple_configuration() -> None:
    assert parse_staples('[" olive oil ", "salt", "olive oil"]') == (
        "olive oil",
        "salt",
    )


def test_api_selected_subset_is_read_only(database: Database) -> None:
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    with TestClient(create_app(database, TOKEN, pantry_staples=("water",))) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        initial = client.get(
            "/v1/shopping-preview?start=2026-10-19&end=2026-10-19"
        ).json()
        assert [item["text"] for item in initial["selected_items"]] == ["1 lemon"]
        assert initial["items"][1]["selected"] is False
        water_key = initial["items"][1]["key"]
        response = client.post(
            "/v1/shopping-preview",
            json={
                "start": "2026-10-19",
                "end": "2026-10-19",
                "selected_keys": [water_key],
            },
        )
        assert response.status_code == 200
        assert [item["text"] for item in response.json()["selected_items"]] == [
            "2 cups water"
        ]
        assert (
            client.post(
                "/v1/shopping-preview",
                json={
                    "start": "2026-10-19",
                    "end": "2026-10-19",
                    "selected_keys": ["obsolete"],
                },
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/v1/shopping-preview",
                json={
                    "start": "2026-10-20",
                    "end": "2026-10-19",
                    "selected_keys": [],
                },
            ).status_code
            == 422
        )
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before
