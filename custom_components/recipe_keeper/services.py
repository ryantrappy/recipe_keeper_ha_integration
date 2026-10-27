"""Response actions for recipes, meal plans, reviewed imports, and cloud edits."""

import sqlite3
from datetime import date
from functools import partial
from uuid import UUID

import probatio as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from pydantic import ValidationError

from ._vendor.recipe_keeper_client.cloud_backend import CacheUnavailable, SyncBusy
from ._vendor.recipe_keeper_client.cloud_edits import (
    EditConflict,
    EditingDisabled,
    EntityNotFound,
)
from ._vendor.recipe_keeper_client.cloud_protocol import ProbeError
from ._vendor.recipe_keeper_client.models import (
    DeleteRequest,
    MealCreateRequest,
    MealUpdateRequest,
    RecipeCreateRequest,
    RecipeUpdateRequest,
)
from ._vendor.recipe_keeper_client.shopping import SelectionChangedError
from .automation_services import ACTION_NAMES, async_setup_automation_services
from .const import CONF_ALLOW_EDITS, CONF_PANTRY_STAPLES, CONF_TODO_ENTITY, DOMAIN

ENTRY = {vol.Required("config_entry_id"): str}
RANGE = {vol.Required("start_date"): cv.date, vol.Required("end_date"): cv.date}
OPERATION = {vol.Required("operation_id"): str}
SERVICE_NAMES = (
    "refresh",
    "get_accounts",
    "search_recipes",
    "get_recipe",
    "get_meal_plan",
    "get_sync_snapshot",
    "preview_ingredients",
    "import_ingredients",
    "get_editing_view",
    "get_edit_operation",
    "reconcile_edit",
    "create_recipe",
    "update_recipe",
    "delete_recipe",
    "create_meal",
    "update_meal",
    "delete_meal",
    *ACTION_NAMES,
)


def date_range(data: dict) -> tuple[date, date]:
    start, end = data["start_date"], data["end_date"]
    if end < start or (end - start).days > 365 or end == date.max:
        raise ServiceValidationError("Use an ordered range of at most 366 days")
    return start, end


async def handle(hass, action: str, call) -> dict:
    if action == "get_accounts":
        return {
            "accounts": [
                {
                    "config_entry_id": entry.entry_id,
                    "title": entry.title,
                    "allow_edits": entry.options.get(CONF_ALLOW_EDITS, False),
                    "todo_entity": entry.options.get(CONF_TODO_ENTITY),
                }
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.state.value == "loaded"
            ]
        }
    entry = service.async_get_config_entry(hass, DOMAIN, call.data["config_entry_id"])
    coordinator = entry.runtime_data
    client = coordinator.client
    data = call.data
    try:
        if action == "refresh":
            await coordinator.async_refresh()
            result = {
                "recipes": coordinator.data.get("recipes"),
                "meals": coordinator.data.get("meals"),
            }
        elif action == "search_recipes":
            result = await client.search(data["query"], data["limit"], data["offset"])
        elif action == "get_recipe":
            recipe = await client.recipe(data["recipe_id"])
            if recipe is None:
                raise ServiceValidationError("Recipe not found")
            result = {"recipe": recipe}
        elif action == "get_meal_plan":
            result = await client.meals(*date_range(data))
        elif action == "get_sync_snapshot":
            result = await client.sync_snapshot()
            return {**result, "stale": result["stale"] or not coordinator.is_fresh}
        elif action == "preview_ingredients":
            result = await client.preview(
                *date_range(data),
                entry.options.get(CONF_PANTRY_STAPLES, []),
                data.get("selected_keys"),
            )
        elif action == "import_ingredients":
            todo_entity = data.get("todo_entity") or entry.options.get(CONF_TODO_ENTITY)
            if not todo_entity:
                raise ServiceValidationError(
                    "Select a to-do list in the action or Configure options"
                )
            import_id = str(UUID(data["import_id"]))
            result = await coordinator.importer.import_items(
                coordinator,
                call,
                *date_range(data),
                data["selected_keys"],
                todo_entity,
                import_id,
                data["acknowledge_issues"],
            )
        elif action == "get_editing_view":
            result = await client.view(data["entity_type"].title(), data["entity_id"])
        elif action == "get_edit_operation":
            result = await client.operation(str(UUID(data["operation_id"])))
            if result is None:
                raise ServiceValidationError("Operation not found")
        elif action == "reconcile_edit":
            result = await client.reconcile(str(UUID(data["operation_id"])))
            await coordinator.async_refresh()
        else:
            if (
                entry.options.get(CONF_ALLOW_EDITS, False) is not True
                or client.allow_edits is not True
            ):
                raise ServiceValidationError(
                    "Enable Allow editing Recipe Keeper in Configure first"
                )
            operation, kind = action.split("_", 1)
            model = {
                ("create", "recipe"): RecipeCreateRequest,
                ("update", "recipe"): RecipeUpdateRequest,
                ("delete", "recipe"): DeleteRequest,
                ("create", "meal"): MealCreateRequest,
                ("update", "meal"): MealUpdateRequest,
                ("delete", "meal"): DeleteRequest,
            }[operation, kind]
            body = {"operation_id": data["operation_id"]}
            if operation != "create":
                body["revision"] = data["revision"]
            if operation != "delete":
                body["changes"] = data["changes"]
            request = model.model_validate(body)
            result = await client.edit(
                kind.title(),
                operation,
                str(request.operation_id),
                request.changes.model_dump(mode="json", exclude_unset=True)
                if operation != "delete"
                else {},
                entity_id=data.get(kind + "_id"),
                expected_revision=body.get("revision"),
            )
            await coordinator.async_refresh()
    except ValidationError:
        raise ServiceValidationError(
            "Invalid edit fields; check the action documentation"
        ) from None
    except (
        EditingDisabled,
        EditConflict,
        EntityNotFound,
        SelectionChangedError,
        SyncBusy,
        ValueError,
    ) as exc:
        raise ServiceValidationError(str(exc)) from None
    except (CacheUnavailable, ProbeError, OSError, sqlite3.Error):
        raise HomeAssistantError(
            "Cloud or cache unavailable; inspect the operation before retrying"
        ) from None
    return {
        **result,
        "stale": not coordinator.is_fresh,
        "last_successful_sync_at": coordinator.data.get("last_successful_sync_at"),
    }


@callback
def async_setup_services(hass) -> None:
    if hass.services.has_service(DOMAIN, "get_accounts"):
        return
    async_setup_automation_services(hass)
    schemas = {
        "refresh": ENTRY,
        "get_accounts": {},
        "search_recipes": {
            **ENTRY,
            vol.Optional("query", default=""): vol.All(str, vol.Length(max=200)),
            vol.Optional("limit", default=25): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
            vol.Optional("offset", default=0): vol.All(
                vol.Coerce(int), vol.Range(min=0)
            ),
        },
        "get_recipe": {**ENTRY, vol.Required("recipe_id"): str},
        "get_meal_plan": {**ENTRY, **RANGE},
        "get_sync_snapshot": ENTRY,
        "preview_ingredients": {
            **ENTRY,
            **RANGE,
            vol.Optional("selected_keys"): vol.All([str], vol.Length(max=10000)),
        },
        "import_ingredients": {
            **ENTRY,
            **RANGE,
            vol.Required("import_id"): str,
            vol.Required("selected_keys"): vol.All([str], vol.Length(max=10000)),
            vol.Optional("todo_entity"): cv.entity_domain("todo"),
            vol.Optional("acknowledge_issues", default=False): bool,
        },
        "get_editing_view": {
            **ENTRY,
            vol.Required("entity_type"): vol.In(("recipe", "meal")),
            vol.Required("entity_id"): str,
        },
        "get_edit_operation": {**ENTRY, **OPERATION},
        "reconcile_edit": {**ENTRY, **OPERATION},
    }
    for kind in ("recipe", "meal"):
        for operation in ("create", "update", "delete"):
            schema = {**ENTRY, **OPERATION}
            if operation != "create":
                schema.update(
                    {vol.Required(kind + "_id"): str, vol.Required("revision"): str}
                )
            if operation != "delete":
                schema[vol.Required("changes")] = dict
            schemas[operation + "_" + kind] = schema
    for name, schema in schemas.items():
        register = (
            service.async_register_admin_service
            if name.startswith(("create_", "update_", "delete_"))
            or name == "import_ingredients"
            else hass.services.async_register
        )
        if register == service.async_register_admin_service:
            register(
                hass,
                DOMAIN,
                name,
                partial(handle, hass, name),
                schema=vol.Schema(schema),
                supports_response=SupportsResponse.ONLY,
            )
        else:
            register(
                DOMAIN,
                name,
                partial(handle, hass, name),
                schema=vol.Schema(schema),
                supports_response=SupportsResponse.ONLY,
            )


@callback
def async_unload_services(hass) -> None:
    for name in SERVICE_NAMES:
        hass.services.async_remove(DOMAIN, name)
