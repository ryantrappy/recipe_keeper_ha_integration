"""Persist explicit to-do imports and hold additions whose outcome is unknown."""

import asyncio
import hashlib
import json
from pathlib import Path

from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.file import write_utf8_file_atomic

from .const import DOMAIN


class ImportLedger:
    """Require a durable commit; HA Store intentionally suppresses write failures."""

    def __init__(self, hass, entry_id: str) -> None:
        self.hass = hass
        self.path = Path(hass.config.path(".storage", f"{DOMAIN}.{entry_id}.imports"))

    def _load(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"items": {}, "imports": {}}
        record = json.loads(raw)
        if record["version"] != 1:
            raise ValueError("Unsupported import ledger version")
        data = record["data"]
        if not isinstance(data["items"], dict) or not isinstance(data["imports"], dict):
            raise ValueError("Invalid import ledger")
        return data

    async def async_load(self) -> dict:
        try:
            return await self.hass.async_add_executor_job(self._load)
        except (OSError, ValueError, KeyError, TypeError):
            raise HomeAssistantError(
                "Shopping import ledger unavailable; restore it from backup"
            ) from None

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_utf8_file_atomic(
            str(self.path), json.dumps({"version": 1, "data": data}), private=True
        )

    async def async_save(self, data: dict) -> None:
        job = self.hass.async_add_executor_job(self._save, data)
        try:
            await asyncio.shield(job)
        except asyncio.CancelledError:
            # Keep the import lock until the atomic write actually finishes.
            await job
            raise


class ShoppingImporter:
    def __init__(self, hass, entry_id: str) -> None:
        self.hass = hass
        self.store = ImportLedger(hass, entry_id)
        self.lock = asyncio.Lock()
        self.ledger = None
        self.closed = False

    def stop(self) -> None:
        self.closed = True

    async def close(self) -> None:
        self.stop()
        async with self.lock:
            pass

    async def import_items(
        self,
        coordinator,
        call,
        start,
        end,
        selected_keys: list[str],
        todo_entity: str,
        import_id: str,
        acknowledge_issues: bool,
    ) -> dict:
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    str(start),
                    str(end),
                    sorted(selected_keys),
                    todo_entity,
                    acknowledge_issues,
                ]
            ).encode()
        ).hexdigest()
        async with self.lock:
            if self.closed or self.hass.state is CoreState.stopping:
                raise ServiceValidationError("Shopping importer is stopping")
            # Reload after any failed save; never trust uncommitted in-memory progress.
            self.ledger = await self.store.async_load()
            previous = self.ledger["imports"].get(import_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ServiceValidationError(
                        "Import ID was already used for a different request"
                    )
                result = previous["result"]
                if result["state"] == "pending":
                    result["state"] = (
                        "uncertain" if result["uncertain_keys"] else "interrupted"
                    )
                    await self.store.async_save(self.ledger)
                return result
            await coordinator.async_refresh()
            if not coordinator.is_fresh:
                raise ServiceValidationError(
                    "Cloud data is stale; shopping import stopped"
                )
            preview = await coordinator.client.preview(
                start,
                end,
                coordinator.entry.options.get("pantry_staples", []),
                selected_keys,
            )
            if preview["issues"] and not acknowledge_issues:
                raise ServiceValidationError(
                    "Preview has issues; review them before importing available items"
                )
            state = self.hass.states.get(todo_entity)
            if state is None or state.state in ("unknown", "unavailable"):
                raise ServiceValidationError("Selected to-do list is unavailable")
            if not int(state.attributes.get("supported_features", 0)) & 1:
                raise ServiceValidationError(
                    "Selected to-do list does not support adding items"
                )
            items = preview["selected_items"]
            result = {
                "import_id": import_id,
                "state": "pending",
                "added_keys": [],
                "skipped_keys": [],
                "uncertain_keys": [],
                "not_attempted_keys": [item["key"] for item in items],
            }
            self.ledger["imports"][import_id] = {
                "fingerprint": fingerprint,
                "planned_keys": list(result["not_attempted_keys"]),
                "result": result,
            }
            await self.store.async_save(self.ledger)
            for item in items:
                if self.closed or self.hass.state is CoreState.stopping:
                    result["state"] = (
                        "uncertain" if result["uncertain_keys"] else "interrupted"
                    )
                    await self.store.async_save(self.ledger)
                    return result
                key = item["key"]
                identity = todo_entity + ":" + key
                result["not_attempted_keys"].remove(key)
                previous_item = self.ledger["items"].get(identity)
                if previous_item:
                    result[
                        "skipped_keys" if previous_item == "added" else "uncertain_keys"
                    ].append(key)
                    continue
                self.ledger["items"][identity] = "pending"
                result["uncertain_keys"].append(key)
                await self.store.async_save(self.ledger)
                try:
                    async with asyncio.timeout(45):
                        await self.hass.services.async_call(
                            "todo",
                            "add_item",
                            {"entity_id": todo_entity, "item": item["text"]},
                            blocking=True,
                            context=call.context,
                        )
                except asyncio.CancelledError:
                    # The committed pending marker blocks replay after cancellation.
                    raise
                except (HomeAssistantError, TimeoutError):
                    self.ledger["items"][identity] = "uncertain"
                    result["state"] = "uncertain"
                    await self.store.async_save(self.ledger)
                    return result
                self.ledger["items"][identity] = "added"
                result["uncertain_keys"].remove(key)
                result["added_keys"].append(key)
                await self.store.async_save(self.ledger)
            result["state"] = "uncertain" if result["uncertain_keys"] else "complete"
            await self.store.async_save(self.ledger)
            return result
