"""Run the installed custom component through an isolated HA runtime."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def custom_component(
    enable_custom_integrations,
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass.config.config_dir = str(tmp_path)
    source = Path(__file__).resolve().parents[1] / "custom_components"
    monkeypatch.setattr(custom_components, "__path__", [str(source)])
    target = Path(hass.config.path("custom_components"))
    target.symlink_to(source, target_is_directory=True)


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    result = MockConfigEntry(
        domain="recipe_keeper",
        title="Recipe Keeper",
        data={"email": "test@example.invalid", "password": "synthetic-password"},
        unique_id="synthetic-account",
        options={"allow_edits": False, "pantry_staples": ["olive oil"]},
    )
    result.add_to_hass(hass)
    return result


@pytest.fixture
def client():
    with patch(
        "custom_components.recipe_keeper.AsyncRecipeClient", autospec=True
    ) as factory:
        instance = factory.return_value
        instance.allow_edits = False
        instance.transport = AsyncMock()
        instance.sync.return_value = {
            "recipes": 1,
            "meals": 1,
            "last_successful_sync_at": "2026-10-20T12:00:00+00:00",
            "calendar_meals": [
                {
                    "id": "m1",
                    "date": "2026-10-21",
                    "title": "Synthetic soup",
                    "meal_type": "Dinner",
                    "notes": "",
                }
            ],
        }
        yield instance
