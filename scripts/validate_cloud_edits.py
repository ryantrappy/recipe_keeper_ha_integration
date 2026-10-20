"""Opt-in live validation restricted to records created by this invocation."""

import argparse
import copy
import json
import os
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from recipe_assistant.cloud_backend import (
    CloudCache,
    CloudSession,
    SyncWorker,
    load_credentials,
)
from recipe_assistant.cloud_edits import CloudEditor
from recipe_assistant.cloud_probe import CloudHTTPError


def private_json(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(value, file)


class OwnedSession(CloudSession):
    def __init__(self, credentials, root: Path, run_id: str) -> None:
        super().__init__(credentials, allow_edits=True)
        self.root = root
        self.run_id = run_id
        self.original_ids: set[str] = set()
        self.owned: dict[str, str] = {}

    def upload(self, cursors: dict[str, str], section: str, record: dict) -> dict:
        entity_id = record["Id"]
        title = f"Recipe Assistant validation {self.run_id} {section}"
        if entity_id in self.original_ids or record.get("Title") != title:
            raise RuntimeError(
                "Refusing to modify a record outside this validation run"
            )
        if entity_id not in self.owned:
            if record.get("IsDeleted") or record.get("LastModified") is not None:
                raise RuntimeError("Only a new record can enter the ownership registry")
            private_json(
                self.root / f"owned-{entity_id}.json",
                {"section": section, "id": entity_id, "title": title},
            )
            self.owned[entity_id] = section
        elif self.owned[entity_id] != section:
            raise RuntimeError("Entity type does not match the ownership registry")
        try:
            return super().upload(cursors, section, record)
        except CloudHTTPError as exc:
            private_json(
                self.root / f"http-failure-{uuid4()}.json",
                {
                    "status": exc.status_code,
                    "retry_after": exc.retry_after,
                    "section": section,
                    "owned_id": entity_id,
                },
            )
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live-test", action="store_true")
    parser.add_argument(
        "--credentials", type=Path, default=Path("local-data/cloud-credentials.json")
    )
    args = parser.parse_args()
    if not args.run_live_test:
        parser.exit(
            0,
            "No cloud requests made. Use --run-live-test to create and clean up "
            "disposable records.\n",
        )
    run_id = str(uuid4())
    root = Path("local-data/two-way-validation") / run_id
    root.mkdir(parents=True, mode=0o700)
    session = OwnedSession(load_credentials(args.credentials), root, run_id)
    before, cursors = session.download(max_pages=100)
    private_json(root / "before.json", {"records": before, "cursors": cursors})
    session.original_ids = {entity_id for rows in before.values() for entity_id in rows}
    cache = CloudCache(root / "cache.db", session.credentials.account_id)
    worker = SyncWorker(cache, session)
    editor = CloudEditor(worker, allow_edits=True)
    report = {"backup_saved": True, "checks": {}, "cleanup_verified": False}

    def settle(result: dict) -> dict:
        for _ in range(2):
            if result["state"] not in ("prepared", "sending", "uncertain"):
                return result
            result = editor.reconcile(result["operation_id"])
        return result

    step = "start"
    try:
        for section in ("Meal", "Recipe"):
            step = section + ".create"
            changes = {"title": f"Recipe Assistant validation {run_id} {section}"}
            if section == "Meal":
                changes["date"] = (date.today() + timedelta(days=30)).isoformat()
            else:
                changes.update(
                    ingredients="1 synthetic test ingredient",
                    directions="Disposable validation record.",
                )
            created = settle(editor.edit(section, "create", str(uuid4()), changes))
            assert created["state"] == "confirmed", created["state"]
            entity_id = created["entity_id"]
            report["checks"][step] = True
            if section == "Recipe":
                step = section + ".seed_preservation_fields"
                seed = editor._record(section, entity_id)
                seed.update(
                    Photo="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1cAAAAASUVORK5CYII=",
                    NutCalories=123,
                    NutProtein=4,
                    Categories=["Integration validation"],
                )
                with cache.sync_lock():
                    session.upload(cache.metadata()["cursors"], section, seed)
                    worker.sync_locked(full=True)
                seeded = editor._record(section, entity_id)
                for key in ("Photo", "NutCalories", "NutProtein", "Categories"):
                    assert seeded.get(key) == seed[key]
            step = section + ".update"
            original = copy.deepcopy(editor._record(section, entity_id))
            view = editor.view(section, entity_id)
            updated = settle(
                editor.edit(
                    section,
                    "update",
                    str(uuid4()),
                    {"notes": "Updated disposable validation record."},
                    entity_id=entity_id,
                    expected_revision=view["revision"],
                )
            )
            assert updated["state"] == "confirmed", updated["state"]
            actual = editor._record(section, entity_id)
            preserved = {
                key: value
                for key, value in original.items()
                if key not in ("Notes", "LastModified")
            }
            assert all(actual.get(key) == value for key, value in preserved.items())
            report["checks"][step] = True
            step = section + ".stale_write_behavior"
            stale = copy.deepcopy(actual)
            competing = {**actual, "Notes": "Competing disposable edit."}
            with cache.sync_lock():
                session.upload(cache.metadata()["cursors"], section, competing)
                worker.sync_locked(full=True)
                stale["Notes"] = "Stale disposable edit."
                try:
                    session.upload(cache.metadata()["cursors"], section, stale)
                except CloudHTTPError as exc:
                    report["checks"][step + ".http_status"] = exc.status_code
                worker.sync_locked(full=True)
            current_note = editor._record(section, entity_id)["Notes"]
            report["checks"][step] = (
                "accepted"
                if current_note == stale["Notes"]
                else "rejected"
                if current_note == competing["Notes"]
                else "unexpected"
            )
            step = section + ".delete"
            view = editor.view(section, entity_id)
            deleted = settle(
                editor.edit(
                    section,
                    "delete",
                    str(uuid4()),
                    {},
                    entity_id=entity_id,
                    expected_revision=view["revision"],
                )
            )
            assert deleted["state"] == "confirmed", deleted["state"]
            report["checks"][step] = True
    except Exception as exc:
        report["failed_step"] = step
        report["failure_type"] = type(exc).__name__
    finally:
        try:
            current, cursors = session.download(max_pages=100)
            for entity_id, section in session.owned.items():
                row = current[section].get(entity_id)
                if row and not row.get("IsDeleted"):
                    session.upload(cursors, section, {**row, "IsDeleted": True})
            after, cursors = session.download(max_pages=100)
            private_json(root / "after.json", {"records": after, "cursors": cursors})
            report["cleanup_verified"] = all(
                after[section].get(entity_id) is None
                or after[section][entity_id].get("IsDeleted") is True
                for entity_id, section in session.owned.items()
            )
            report["existing_recipes_unchanged"] = all(
                after["Recipe"].get(key) == value
                for key, value in before["Recipe"].items()
            )
            report["existing_meals_unchanged"] = all(
                after["Meal"].get(key) == value for key, value in before["Meal"].items()
            )
        except Exception as exc:
            report["cleanup_error"] = type(exc).__name__
        private_json(root / "report.json", report)
        print(json.dumps({**report, "private_backup": str(root)}, indent=2))
    if (
        "failed_step" in report
        or not report["cleanup_verified"]
        or not report.get("existing_recipes_unchanged")
        or not report.get("existing_meals_unchanged")
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
