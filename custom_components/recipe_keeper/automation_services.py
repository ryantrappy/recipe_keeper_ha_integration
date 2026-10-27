"""Mealie-style automation actions backed by the shared guarded client."""

import sqlite3
from functools import partial
from uuid import UUID

import probatio as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from homeassistant.util import dt as dt_util
from pydantic import ValidationError

from ._vendor.recipe_keeper_client.automation import MEAL_TYPES
from ._vendor.recipe_keeper_client.cloud_backend import CacheUnavailable, SyncBusy
from ._vendor.recipe_keeper_client.cloud_edits import (
    EditConflict,
    EditingDisabled,
    EntityNotFound,
)
from ._vendor.recipe_keeper_client.cloud_protocol import ProbeError
from .const import CONF_ALLOW_EDITS, CONF_TODO_ENTITY, DOMAIN

READ_ACTIONS = ("get_recipes", "get_mealplan", "get_shopping_list_items")
WRITE_ACTIONS = (
    "set_mealplan",
    "set_random_mealplan",
    "update_mealplan",
    "delete_mealplan",
    "import_recipe",
)
ACTION_NAMES = (*READ_ACTIONS, *WRITE_ACTIONS)


async def handle(hass, action: str, call) -> dict:
    entry = service.async_get_config_entry(hass, DOMAIN, call.data["config_entry_id"])
    coordinator = entry.runtime_data
    client = coordinator.client
    data = call.data
    if action in WRITE_ACTIONS and (
        entry.options.get(CONF_ALLOW_EDITS, False) is not True
        or client.allow_edits is not True
    ):
        raise ServiceValidationError(
            "Enable Allow editing Recipe Keeper in Configure first"
        )
    try:
        if action == "get_recipes":
            result = {
                "recipes": await client.search(
                    data["search_terms"], data["result_limit"], data["offset"]
                )
            }
        elif action == "get_mealplan":
            from .services import date_range

            today = dt_util.now().date()
            bounds = {
                "start_date": data.get("start_date", today),
                "end_date": data.get("end_date", today),
            }
            result = await client.mealplans(*date_range(bounds))
        elif action == "get_shopping_list_items":
            entity_id = data.get("todo_entity") or entry.options.get(CONF_TODO_ENTITY)
            if not entity_id:
                raise ServiceValidationError(
                    "Select a to-do list in the action or Configure options"
                )
            response = await hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": entity_id, "status": data["status"]},
                blocking=True,
                return_response=True,
                context=call.context,
            )
            if not response or entity_id not in response:
                raise ServiceValidationError("Selected to-do list is unavailable")
            return {
                "todo_entity": entity_id,
                "source": "home_assistant",
                "items": response[entity_id]["items"],
            }
        elif action == "import_recipe":
            result = await client.import_recipe(
                str(UUID(data["operation_id"])),
                data["url"],
                include_tags=data["include_tags"],
                recipe_index=data.get("recipe_index"),
            )
            await coordinator.async_refresh()
        elif action == "delete_mealplan":
            result = await client.edit(
                "Meal",
                "delete",
                str(UUID(data["operation_id"])),
                {},
                entity_id=data["mealplan_id"],
                expected_revision=data["revision"],
            )
            result = {**result, "mealplan_id": result["entity_id"]}
            await coordinator.async_refresh()
        else:
            intent = {
                "action": action,
                "date": data["date"].isoformat(),
                "entry_type": data["entry_type"],
            }
            if action == "set_random_mealplan":
                intent.update(
                    search_terms=data["search_terms"],
                    favourite_only=data["favourite_only"],
                )
            else:
                intent.update(
                    recipe_id=data.get("recipe_id"),
                    note_title=data.get("note_title"),
                    note_text=data.get("note_text"),
                )
                if action == "update_mealplan":
                    intent.update(
                        mealplan_id=data["mealplan_id"], revision=data["revision"]
                    )
            result = await client.plan_meal(str(UUID(data["operation_id"])), intent)
            await coordinator.async_refresh()
    except ValidationError:
        raise ServiceValidationError("Invalid recipe or meal fields") from None
    except (EditingDisabled, EditConflict, EntityNotFound, SyncBusy, ValueError) as exc:
        raise ServiceValidationError(str(exc)) from None
    except (CacheUnavailable, ProbeError, OSError, sqlite3.Error):
        raise HomeAssistantError(
            "Cloud or cache unavailable; inspect the operation before retrying"
        ) from None
    return {
        **result,
        "stale": result.get("stale", False) or not coordinator.is_fresh,
        "last_successful_sync_at": result.get(
            "last_successful_sync_at", coordinator.data.get("last_successful_sync_at")
        ),
    }


@callback
def async_setup_automation_services(hass) -> None:
    entry = {vol.Required("config_entry_id"): str}
    operation = {**entry, vol.Required("operation_id"): str}
    existing = {
        **operation,
        vol.Required("mealplan_id"): str,
        vol.Required("revision"): vol.All(str, vol.Match(r"^[0-9a-f]{64}$")),
    }
    plan = {
        vol.Required("date"): cv.date,
        vol.Required("entry_type"): vol.In(MEAL_TYPES),
    }
    text = vol.All(str, vol.Length(min=1, max=1000))

    def selection(base: dict):
        return vol.Any(
            vol.Schema({**base, **plan, vol.Required("recipe_id"): text}),
            vol.Schema(
                {
                    **base,
                    **plan,
                    vol.Required("note_title"): text,
                    vol.Optional("note_text"): vol.All(str, vol.Length(max=100000)),
                }
            ),
        )

    schemas = {
        "get_recipes": vol.Schema(
            {
                **entry,
                vol.Optional("search_terms", default=""): vol.All(
                    str, vol.Length(max=200)
                ),
                vol.Optional("result_limit", default=10): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=100)
                ),
                vol.Optional("offset", default=0): vol.All(
                    vol.Coerce(int), vol.Range(min=0)
                ),
            }
        ),
        "get_mealplan": vol.Schema(
            {
                **entry,
                vol.Optional("start_date"): cv.date,
                vol.Optional("end_date"): cv.date,
            }
        ),
        "get_shopping_list_items": vol.Schema(
            {
                **entry,
                vol.Optional("todo_entity"): cv.entity_domain("todo"),
                vol.Optional("status", default=["needs_action"]): [
                    vol.In(("needs_action", "completed"))
                ],
            }
        ),
        "import_recipe": vol.Schema(
            {
                **operation,
                vol.Required("url"): vol.All(str, vol.Length(max=10000)),
                vol.Optional("include_tags", default=False): bool,
                vol.Optional("recipe_index"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=1000)
                ),
            }
        ),
        "set_mealplan": selection(operation),
        "update_mealplan": selection(existing),
        "set_random_mealplan": vol.Schema(
            {
                **operation,
                **plan,
                vol.Optional("search_terms", default=""): vol.All(
                    str, vol.Length(max=200)
                ),
                vol.Optional("favourite_only", default=False): bool,
            }
        ),
        "delete_mealplan": vol.Schema(existing),
    }
    for name, schema in schemas.items():
        handler = partial(handle, hass, name)
        if name in WRITE_ACTIONS:
            service.async_register_admin_service(
                hass,
                DOMAIN,
                name,
                handler,
                schema=schema,
                supports_response=SupportsResponse.ONLY,
            )
        else:
            hass.services.async_register(
                DOMAIN,
                name,
                handler,
                schema=schema,
                supports_response=SupportsResponse.ONLY,
            )
