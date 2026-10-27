"""Explicit cloud edits with durable intent and no automatic upload replay."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from .cloud_backend import SyncWorker
from .cloud_protocol import CloudHTTPError, ProbeError

RECIPE_FIELDS = {
    "title": "Title",
    "serving_size": "ServingSize",
    "source": "Source",
    "ingredients": "Ingredients",
    "directions": "Directions",
    "notes": "Notes",
    "favourite": "IsFavourite",
    "categories": "Categories",
    "rating": "Rating",
    "preparation_time_raw": "PreparationTime",
    "cooking_time_raw": "CookingTime",
}
MEAL_FIELDS = {
    "title": "Title",
    "date": "MealDate",
    "meal_type": "MealTypeName",
    "meal_type_code": "MealType",
    "scale": "Scale",
    "notes": "Notes",
    "position": "Position",
    "meal_time_raw": "MealTime",
    "colour_raw": "Colour",
}
FIELDS = {"Recipe": RECIPE_FIELDS, "Meal": MEAL_FIELDS}


class EditingDisabled(Exception):
    """The account's editing policy forbids cloud writes."""

    def __init__(self, message: str, *, request_started: bool = False) -> None:
        super().__init__(message)
        self.request_started = request_started


class EditConflict(Exception):
    """The requested baseline or operation ID cannot be used."""


class EntityNotFound(Exception):
    """No active remote record has that ID."""


def revision(record: dict) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def equivalent(actual: dict, intended: dict) -> bool:
    if intended.get("IsDeleted"):
        return actual.get("Id") == intended["Id"] and actual.get("IsDeleted") is True
    if actual.get("IsDeleted"):
        return False
    actual = {key: value for key, value in actual.items() if key != "LastModified"}
    intended = {key: value for key, value in intended.items() if key != "LastModified"}
    for record in (actual, intended):
        if isinstance(record.get("MealDate"), str):
            try:
                record["MealDate"] = (
                    datetime.fromisoformat(record["MealDate"])
                    .replace(tzinfo=None)
                    .isoformat()
                )
            except ValueError:
                return False
        if isinstance(record.get("AddedDate"), str):
            try:
                record["AddedDate"] = datetime.fromisoformat(record["AddedDate"])
            except ValueError:
                return False
    return actual == intended


def new_record(section: str, entity_id: str) -> dict:
    record = {"Id": entity_id, "LastModified": None, "IsDeleted": False}
    if section == "Meal":
        return {
            **record,
            "MealDate": "",
            "MealType": 0,
            "MealTypeName": "Dinner",
            "Title": "",
            "Position": 0,
            "MealTime": 0,
            "Colour": 0,
            "Scale": "1",
            "Notes": "",
        }
    return {
        **record,
        "Title": "",
        "Course": "",
        "Courses": [],
        "Categories": [],
        "Collections": [],
        "Source": "",
        "ServingSize": "",
        "PreparationTime": 0,
        "CookingTime": 0,
        "Ingredients": "",
        "Directions": "",
        "Notes": "",
        "Rating": 0,
        "IsFavourite": False,
        "ShareId": None,
        "NutServingSize": "",
        **dict.fromkeys(("Photo", *(f"Photo{i}" for i in range(1, 10)))),
        **dict.fromkeys(
            (
                "NutCalories",
                "NutTotalFat",
                "NutSaturatedFat",
                "NutCholesterol",
                "NutSodium",
                "NutTotalCarbohydrate",
                "NutDietaryFiber",
                "NutSugars",
                "NutProtein",
            )
        ),
        "LinkedRecipeIds": [],
        "AddedDate": datetime.now(UTC).isoformat(),
    }


class CloudEditor:
    def __init__(self, worker: SyncWorker, *, allow_edits: bool = False) -> None:
        if type(allow_edits) is not bool:
            raise ValueError("allow_edits must be a boolean")
        self.worker = worker
        self.cache = worker.cache
        self.allow_edits = allow_edits

    @property
    def enabled(self) -> bool:
        return self.allow_edits is True and self.worker.session.allow_edits is True

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise EditingDisabled("Recipe Keeper editing is disabled")

    def _record(self, section: str, entity_id: str) -> dict | None:
        with self.cache.connect() as conn:
            row = conn.execute(
                "SELECT Record FROM RawRecords WHERE Section=? AND Id=?",
                (section, entity_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def view(self, section: str, entity_id: str) -> dict:
        fields = FIELDS[section]
        record = self._record(section, entity_id)
        if not record or record.get("IsDeleted"):
            raise EntityNotFound("Entity not found")
        if not isinstance(record.get("LastModified"), str):
            raise EditConflict("A complete cloud refresh is required before editing")
        values = {name: record.get(key) for name, key in fields.items()}
        if section == "Meal":
            values["date"] = (
                datetime.fromisoformat(record["MealDate"]).date().isoformat()
            )
        return {
            "id": entity_id,
            "revision": revision(record),
            "fields": values,
        }

    @staticmethod
    def _result(row: sqlite3.Row | tuple) -> dict:
        return {
            "operation_id": row[0],
            "entity_type": row[2],
            "entity_id": row[3],
            "action": row[4],
            "state": row[7],
            "error": row[8],
            "created_at": row[9],
        }

    def operation(self, operation_id: str) -> dict | None:
        with self.cache.connect() as conn:
            row = conn.execute(
                "SELECT * FROM EditOperations WHERE Id=?", (operation_id,)
            ).fetchone()
        return self._result(row) if row else None

    def counts(self) -> dict[str, int]:
        with self.cache.connect() as conn:
            return dict(
                conn.execute(
                    "SELECT State, count(*) FROM EditOperations GROUP BY State"
                )
            )

    def _state(self, operation_id: str, state: str, error: str | None = None) -> None:
        with self.cache.connect() as conn:
            conn.execute(
                "UPDATE EditOperations SET State=?, Error=? WHERE Id=?",
                (state, error, operation_id),
            )

    def edit(
        self,
        section: str,
        action: str,
        operation_id: str,
        changes: dict,
        *,
        entity_id: str | None = None,
        expected_revision: str | None = None,
        preflight: Callable[[], None] | None = None,
    ) -> dict:
        self._require_enabled()
        if section not in FIELDS or action not in ("create", "update", "delete"):
            raise ValueError("Unsupported edit operation")
        if set(changes) - FIELDS[section].keys():
            raise ValueError("Unsupported edit fields")
        fingerprint = revision(
            {
                "section": section,
                "action": action,
                "entity_id": entity_id,
                "revision": expected_revision,
                "changes": changes,
            }
        )
        with self.cache.sync_lock():
            with self.cache.connect() as conn:
                previous = conn.execute(
                    "SELECT * FROM EditOperations WHERE Id=?", (operation_id,)
                ).fetchone()
            if previous:
                if previous[1] != fingerprint:
                    raise EditConflict("Operation ID was used for a different request")
                return self._result(previous)
            self.worker.sync_locked()
            self._require_enabled()
            if preflight is not None:
                preflight()
            baseline = None
            if action == "create":
                entity_id = str(uuid4())
                payload = new_record(section, entity_id)
            else:
                baseline = self._record(section, entity_id)
                if not baseline or baseline.get("IsDeleted"):
                    raise EntityNotFound("Entity not found")
                if (
                    not isinstance(baseline.get("LastModified"), str)
                    or revision(baseline) != expected_revision
                ):
                    raise EditConflict(
                        "Record changed; fetch a new editing view and review it"
                    )
                payload = dict(baseline)
            with self.cache.connect() as conn:
                pending = conn.execute(
                    "SELECT Id FROM EditOperations WHERE Section=? AND EntityId=? "
                    "AND State IN ('prepared', 'sending', 'uncertain')",
                    (section, entity_id),
                ).fetchone()
            if pending:
                raise EditConflict("An unresolved operation exists for this record")
            for name, value in changes.items():
                if section == "Meal" and name == "date":
                    value += "T00:00:00"
                payload[FIELDS[section][name]] = value
            if action == "delete":
                payload["IsDeleted"] = True
            with self.cache.connect() as conn:
                conn.execute(
                    "INSERT INTO EditOperations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        fingerprint,
                        section,
                        entity_id,
                        action,
                        json.dumps(baseline),
                        json.dumps(payload),
                        "prepared",
                        None,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            if not self.enabled:
                self._state(operation_id, "cancelled", "editing_disabled_before_upload")
                return self.operation(operation_id)
            self._state(operation_id, "sending")
            try:
                response = self.worker.session.upload(
                    self.cache.metadata()["cursors"], section, payload
                )
                part = response.get(section + "SyncEntityResponse", {})
                if not isinstance(part, dict):
                    raise ProbeError("Upload response section is invalid")
                acks = part.get("UpdatedLocalItems")
                if (
                    not isinstance(acks, list)
                    or len(acks) != 1
                    or not isinstance(acks[0], dict)
                    or acks[0].get("Id") != entity_id
                    or not isinstance(acks[0].get("LastModified"), str)
                ):
                    raise ProbeError("Upload acknowledgement incomplete")
            except EditingDisabled as exc:
                if not exc.request_started:
                    self._state(
                        operation_id, "cancelled", "editing_disabled_before_upload"
                    )
                    return self.operation(operation_id)
                self._state(operation_id, "uncertain", "editing_disabled_after_request")
            except CloudHTTPError as exc:
                self._state(operation_id, "uncertain", f"upload_http_{exc.status_code}")
            except (ProbeError, OSError, ValueError, TypeError):
                self._state(operation_id, "uncertain", "upload_outcome_unknown")
            else:
                self._state(operation_id, "uncertain", "verification_required")
            try:
                self._reconcile_locked(operation_id)
            except (ProbeError, OSError, sqlite3.Error, ValueError, TypeError):
                pass
            return self.operation(operation_id)

    def _reconcile_locked(self, operation_id: str) -> None:
        with self.cache.connect() as conn:
            row = conn.execute(
                "SELECT * FROM EditOperations WHERE Id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise EntityNotFound("Operation not found")
        if row[7] == "prepared":
            self._state(operation_id, "cancelled", "interrupted_before_upload")
            return
        if row[7] not in ("prepared", "sending", "uncertain"):
            return
        self.worker.sync_locked(full=True)
        actual = self._record(row[2], row[3])
        intended = json.loads(row[6])
        baseline = json.loads(row[5])
        if actual is not None and equivalent(actual, intended):
            self._state(operation_id, "confirmed")
        elif actual != baseline and (actual is not None or baseline is not None):
            self._state(operation_id, "conflict", "remote_differs_from_intent")

    def reconcile(self, operation_id: str) -> dict:
        with self.cache.sync_lock():
            self._reconcile_locked(operation_id)
            return self.operation(operation_id)
