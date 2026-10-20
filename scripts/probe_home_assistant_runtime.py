"""Verify native HA networking, cache storage, and actions in an isolated instance.

Run with a Home Assistant development environment's Python, not the API venv.
This is a feasibility harness, not an installable custom integration.
"""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from homeassistant.components import network
from homeassistant.components.calendar import CalendarEvent
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import frame
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://recipekeeper.azurewebsites.net"
SECTIONS = ("Recipe", "Meal")
INITIAL_CURSOR = "1601-01-01T00:00:00Z"
DOMAIN = "recipe_keeper_feasibility"


def read_credentials(path: Path) -> dict:
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Credentials file must have mode 0600")
    data = json.loads(path.read_text())
    return {
        "username": data["email"],
        "password": data["password"],
        "grant_type": "password",
    }


def decode_sync(raw: bytes) -> dict:
    data = json.loads(raw)
    for section in SECTIONS:
        part = data.get(section + "SyncEntityResponse")
        if isinstance(part, dict):
            part["UpdatedServerItems"] = [
                {
                    key: value
                    for key, value in row.items()
                    if not key.startswith("Photo")
                }
                for row in part["UpdatedServerItems"]
            ]
    return data


async def verify(credentials_path: Path, config_dir: Path) -> dict:
    await asyncio.to_thread(config_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
    hass = HomeAssistant(str(config_dir))
    frame.async_setup(hass)
    try:
        await network.async_get_adapters(hass)
        session = async_get_clientsession(hass)
        credentials = await hass.async_add_executor_job(
            read_credentials, credentials_path
        )
        account_id = hashlib.sha256(
            credentials["username"].casefold().encode()
        ).hexdigest()
        async with session.post(
            BASE_URL + "/token",
            data=credentials,
            allow_redirects=False,
            headers={"X-RK-APIVersion": "10"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Fresh login failed: HTTP {response.status}")
            auth = await response.json()
        token = auth["access_token"]
        del credentials, auth
        store = Store[dict](
            hass,
            1,
            DOMAIN,
            private=True,
            atomic_writes=True,
            serialize_in_event_loop=False,
        )
        previous = await store.async_load() or {}
        if previous and previous["account_id"] != account_id:
            raise RuntimeError("Feasibility cache belongs to another account")
        cursors = dict(previous.get("cursors", {}))
        records = {
            name: dict(previous.get("entities", {}).get(name, {})) for name in SECTIONS
        }
        pending = {name: cursors.get(name, INITIAL_CURSOR) for name in SECTIONS}
        for _ in range(100):
            payload = {
                name + "SyncEntityRequest": {
                    "MaxLastModified": cursor,
                    "SyncEntities": [],
                    "LocalChangesIds": [],
                }
                for name, cursor in pending.items()
            }
            async with session.post(
                BASE_URL + "/api/sync",
                json=payload,
                allow_redirects=False,
                headers={"Authorization": "Bearer " + token, "X-RK-APIVersion": "10"},
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"Cloud download failed: HTTP {response.status}")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 50_000_000:
                        raise RuntimeError("Cloud response exceeds the probe limit")
            data = await hass.async_add_executor_job(decode_sync, bytes(raw))
            for name, old_cursor in list(pending.items()):
                part = data[name + "SyncEntityResponse"]
                if part["UpdatedLocalItems"]:
                    raise RuntimeError("Unexpected upload acknowledgements")
                for row in part["UpdatedServerItems"]:
                    if row["IsDeleted"]:
                        records[name].pop(row["Id"], None)
                    else:
                        records[name][row["Id"]] = row
                cursor = part["MaxLastModified"]
                cursors[name] = cursor
                if part["HasMoreUpdatedServerItems"]:
                    if old_cursor == cursor:
                        raise RuntimeError("Pagination did not advance")
                    pending[name] = cursor
                else:
                    del pending[name]
            if not pending:
                break
        if pending:
            raise RuntimeError("Pagination limit reached")
        snapshot = {"account_id": account_id, "entities": records, "cursors": cursors}
        await store.async_save(snapshot)
        reopened = Store[dict](hass, 1, DOMAIN, private=True, atomic_writes=True)
        if await reopened.async_load() != snapshot:
            raise RuntimeError("Home Assistant cache reload did not match")

        async def search(call: ServiceCall) -> ServiceResponse:
            query = call.data["query"].casefold()
            return {
                "recipes": [
                    {"id": row["Id"], "title": row["Title"]}
                    for row in records["Recipe"].values()
                    if query in row["Title"].casefold()
                ]
            }

        async def get_recipe(call: ServiceCall) -> ServiceResponse:
            return {"recipe": records["Recipe"][call.data["recipe_id"]]}

        hass.services.async_register(
            DOMAIN, "search", search, supports_response=SupportsResponse.ONLY
        )
        hass.services.async_register(
            DOMAIN, "get_recipe", get_recipe, supports_response=SupportsResponse.ONLY
        )
        search_result = await hass.services.async_call(
            DOMAIN,
            "search",
            {"query": ""},
            blocking=True,
            return_response=True,
        )
        recipe_response_verified = False
        if records["Recipe"]:
            recipe_id = next(iter(records["Recipe"]))
            contents = await hass.services.async_call(
                DOMAIN,
                "get_recipe",
                {"recipe_id": recipe_id},
                blocking=True,
                return_response=True,
            )
            recipe_response_verified = (
                "Ingredients" in contents["recipe"]
                and "Directions" in contents["recipe"]
            )
        events = []
        for meal in records["Meal"].values():
            start = datetime.fromisoformat(meal["MealDate"]).date()
            events.append(
                CalendarEvent(
                    start=start,
                    end=start + timedelta(days=1),
                    summary=meal["Title"],
                    uid=meal["Id"],
                )
            )
        return {
            "home_assistant_shared_session": True,
            "fresh_login": "succeeded",
            "recipes": len(records["Recipe"]),
            "meal_calendar_events": len(events),
            "ha_storage_reload": "matched",
            "search_action_results": len(search_result["recipes"]),
            "recipe_response_contents": recipe_response_verified,
            "uploaded_entities": 0,
            "external_api_server_used": False,
        }
    finally:
        await hass.async_stop(force=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=ROOT / "local-data/cloud-credentials.json",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=ROOT / "local-data/ha-feasibility"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(verify(args.credentials_file, args.config_dir))
    except (
        aiohttp.ClientError,
        TimeoutError,
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
    ) as exc:
        raise SystemExit(
            f"HA runtime probe failed ({type(exc).__name__}); private details omitted"
        ) from None
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
