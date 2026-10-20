"""Apply pantry defaults and explicit choices without altering recipe contents."""

import json
import re
import unicodedata
from collections.abc import Sequence

from .models import ShoppingPreview

PREFIX_WORDS = frozenset(
    "a of cup cups tbsp tbs tablespoon tablespoons tsp teaspoon teaspoons "
    "g gram grams kg kilogram kilograms ml milliliter milliliters millilitre "
    "millilitres l liter liters litre litres oz ounce ounces lb lbs pound pounds "
    "fl fluid pinch pinches dash dashes drizzle extra virgin pure light refined "
    "unrefined organic cold pressed fresh freshly ground finely coarsely coarse "
    "kosher sea table unsalted salted granulated white black".split()
)


class SelectionChangedError(ValueError):
    """A selected key no longer exists in the regenerated preview."""


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[-–—]", " ", value).split())


def pantry_match(text: str, staple: str) -> bool:
    ingredient, phrase = normalize(text), normalize(staple)
    if not phrase:
        return False
    match = re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", ingredient)
    if match is None:
        return False
    prefix, suffix = (
        ingredient[: match.start()].strip(),
        ingredient[match.end() :].strip(),
    )
    prefix_words = re.findall(r"[^\s.,]+", prefix)
    if any(
        word not in PREFIX_WORDS and not re.fullmatch(r"[\d/⁄]+", word)
        for word in prefix_words
    ):
        return False
    return (
        not suffix
        or suffix.startswith((",", "("))
        or bool(
            re.match(
                r"(?:for\b|to taste\b|as needed\b|plus\b|divided\b|optional\b)", suffix
            )
        )
    )


def parse_staples(value: str) -> tuple[str, ...]:
    entries = json.loads(value)
    if (
        not isinstance(entries, list)
        or len(entries) > 500
        or any(
            not isinstance(entry, str) or not entry.strip() or len(entry) > 100
            for entry in entries
        )
    ):
        raise ValueError(
            "RECIPE_ASSISTANT_PANTRY_STAPLES must be a JSON list of ingredient names"
        )
    return tuple(dict.fromkeys(entry.strip() for entry in entries))


def select_ingredients(
    preview: ShoppingPreview,
    staples: Sequence[str] = (),
    selected_keys: Sequence[str] | None = None,
) -> ShoppingPreview:
    available = {item.key for item in preview.items}
    explicit = set(selected_keys) if selected_keys is not None else None
    if explicit is not None and explicit - available:
        raise SelectionChangedError(
            "Ingredient preview changed; refresh and select again"
        )
    choices = []
    for item in preview.items:
        staple = next((name for name in staples if pantry_match(item.text, name)), None)
        selected = item.key in explicit if explicit is not None else staple is None
        choices.append(
            item.model_copy(update={"selected": selected, "pantry_match": staple})
        )
    return preview.model_copy(update={"items": choices})
