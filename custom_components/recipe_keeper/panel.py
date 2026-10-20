"""Serve a self-contained recipe and ingredient selection panel."""

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig


async def async_setup_panel(hass) -> None:
    if "frontend" not in hass.config.components:
        return
    if not hass.data.get("recipe_keeper_static_registered"):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    "/recipe_keeper_static",
                    str(Path(__file__).with_name("frontend")),
                    True,
                )
            ]
        )
        hass.data["recipe_keeper_static_registered"] = True
    if not hass.data.get("recipe_keeper_panel_registered"):
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path="recipe-keeper",
            webcomponent_name="recipe-keeper-panel",
            sidebar_title="Recipe Keeper",
            sidebar_icon="mdi:book-open-page-variant",
            module_url="/recipe_keeper_static/recipe-keeper-panel.js?v=0.2.0",
            embed_iframe=False,
        )
        hass.data["recipe_keeper_panel_registered"] = True


def async_unload_panel(hass) -> None:
    if hass.data.pop("recipe_keeper_panel_registered", False):
        frontend.async_remove_panel(hass, "recipe-keeper")
