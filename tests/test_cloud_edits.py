"""Two-way sync tests use a synthetic cloud and never real recipes."""

import copy
import json
from datetime import UTC, datetime
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from recipe_assistant import cloud_probe
from recipe_assistant.api import create_app
from recipe_assistant.cloud_backend import (
    CloudCache,
    CloudDatabase,
    CloudSession,
    SyncWorker,
)
from recipe_assistant.cloud_edits import (
    CloudEditor,
    EditConflict,
    EditingDisabled,
    EntityNotFound,
    revision,
)
from recipe_assistant.cloud_probe import (
    INITIAL_CURSOR,
    SECTIONS,
    CloudHTTPError,
    Page,
    ProbeError,
)

from .conftest import TOKEN
from .test_cloud_backend import CREDENTIALS, CURSOR, MEAL, RECIPE


class FakeCloud:
    def __init__(self) -> None:
        self.allow_edits = True
        self.records = {
            "Recipe": {
                "r1": {
                    **RECIPE,
                    "LastModified": CURSOR,
                    "IsDeleted": False,
                    "Photo": "synthetic-photo",
                    "Photo9": "ninth-photo",
                    "NutCalories": 123,
                    "ShareId": "keep-sharing",
                    "UnknownField": {"nested": [1, 2, 3]},
                }
            },
            "Meal": {
                "m1": {
                    **MEAL,
                    "LastModified": CURSOR,
                    "IsDeleted": False,
                    "MealType": 3,
                    "Colour": 42,
                    "MealTime": 0,
                }
            },
        }
        self.uploads: list[tuple[str, dict]] = []
        self.downloads: list[dict] = []
        self.apply_upload = True
        self.upload_error: Exception | None = None
        self.verification_error = False
        self.malformed_part = False
        self.upload_part: object = None

    def page(self, cursors: dict[str, str]) -> dict[str, Page]:
        if self.verification_error and self.uploads:
            raise ProbeError("Synthetic outage after upload")
        self.downloads.append(dict(cursors))
        return {
            section: Page(
                copy.deepcopy(list(self.records[section].values())), CURSOR, False
            )
            for section in cursors
        }

    def upload(self, cursors: dict[str, str], section: str, record: dict) -> dict:
        if not self.allow_edits:
            raise EditingDisabled("disabled")
        self.uploads.append((section, copy.deepcopy(record)))
        if self.apply_upload:
            self.records[section][record["Id"]] = {
                **copy.deepcopy(record),
                "LastModified": "2026-10-20T00:00:00.0000001Z",
            }
        if self.upload_error:
            raise self.upload_error
        if self.malformed_part:
            return {section + "SyncEntityResponse": self.upload_part}
        return {
            section + "SyncEntityResponse": {
                "UpdatedLocalItems": [
                    {"Id": record["Id"], "LastModified": "2026-10-20T00:00:00.0000001Z"}
                ]
            }
        }


@pytest.fixture
def editor(tmp_path: Path) -> CloudEditor:
    cache = CloudCache(tmp_path / "cache.db", CREDENTIALS.account_id)
    cloud = FakeCloud()
    worker = SyncWorker(cache, cloud)
    worker.sync()
    return CloudEditor(worker, allow_edits=True)


def test_update_preserves_every_unedited_field_and_durable_baseline(
    editor: CloudEditor,
) -> None:
    before = copy.deepcopy(editor.worker.session.records["Recipe"]["r1"])
    view = editor.view("Recipe", "r1")
    operation_id = str(uuid4())
    result = editor.edit(
        "Recipe",
        "update",
        operation_id,
        {"notes": "New note"},
        entity_id="r1",
        expected_revision=view["revision"],
    )
    assert result["state"] == "confirmed"
    expected = {**before, "Notes": "New note"}
    assert editor.worker.session.uploads == [("Recipe", expected)]
    with editor.cache.connect() as conn:
        row = conn.execute(
            "SELECT Baseline, Payload FROM EditOperations WHERE Id=?", (operation_id,)
        ).fetchone()
    assert json.loads(row[0]) == before
    assert json.loads(row[1]) == expected
    assert "synthetic-photo" not in json.dumps(view)
    assert "synthetic-photo" not in json.dumps(result)
    assert editor.worker.session.downloads[-1] == dict.fromkeys(
        SECTIONS, INITIAL_CURSOR
    )


@pytest.mark.parametrize("section", ["Recipe", "Meal"])
def test_create_update_delete_lifecycle(editor: CloudEditor, section: str) -> None:
    changes = {"title": "Disposable synthetic record"}
    if section == "Meal":
        changes["date"] = "2026-10-22"
    created = editor.edit(section, "create", str(uuid4()), changes)
    assert created["state"] == "confirmed"
    view = editor.view(section, created["entity_id"])
    updated = editor.edit(
        section,
        "update",
        str(uuid4()),
        {"notes": "Edited"},
        entity_id=view["id"],
        expected_revision=view["revision"],
    )
    assert updated["state"] == "confirmed"
    view = editor.view(section, view["id"])
    deleted = editor.edit(
        section,
        "delete",
        str(uuid4()),
        {},
        entity_id=view["id"],
        expected_revision=view["revision"],
    )
    assert deleted["state"] == "confirmed"
    with pytest.raises(EntityNotFound):
        editor.view(section, view["id"])


def test_stale_revision_never_uploads(editor: CloudEditor) -> None:
    view = editor.view("Recipe", "r1")
    editor.worker.session.records["Recipe"]["r1"]["Directions"] = (
        "Changed on another device"
    )
    with pytest.raises(EditConflict):
        editor.edit(
            "Recipe",
            "update",
            str(uuid4()),
            {"notes": "new"},
            entity_id="r1",
            expected_revision=view["revision"],
        )
    assert editor.worker.session.uploads == []


def test_deleted_remote_record_is_not_resurrected(editor: CloudEditor) -> None:
    view = editor.view("Recipe", "r1")
    editor.worker.session.records["Recipe"]["r1"]["IsDeleted"] = True
    with pytest.raises(EntityNotFound):
        editor.edit(
            "Recipe",
            "update",
            str(uuid4()),
            {"notes": "new"},
            entity_id="r1",
            expected_revision=view["revision"],
        )
    assert editor.worker.session.uploads == []


def test_same_operation_is_idempotent_and_different_request_is_rejected(
    editor: CloudEditor,
) -> None:
    operation_id = str(uuid4())
    first = editor.edit("Recipe", "create", operation_id, {"title": "Test"})
    repeated = editor.edit("Recipe", "create", operation_id, {"title": "Test"})
    assert repeated == first
    assert len(editor.worker.session.uploads) == 1
    with pytest.raises(EditConflict):
        editor.edit("Recipe", "create", operation_id, {"title": "Different"})


def test_timeout_after_commit_is_confirmed_by_independent_download(
    editor: CloudEditor,
) -> None:
    editor.worker.session.upload_error = ProbeError("timeout after commit")
    result = editor.edit("Recipe", "create", str(uuid4()), {"title": "Test"})
    assert result["state"] == "confirmed"
    assert len(editor.worker.session.uploads) == 1


def test_timeout_before_commit_is_held_across_restart_and_disable(
    editor: CloudEditor,
) -> None:
    cloud = editor.worker.session
    cloud.apply_upload = False
    cloud.upload_error = ProbeError("timeout before commit")
    operation_id = str(uuid4())
    result = editor.edit("Recipe", "create", operation_id, {"title": "Test"})
    assert result["state"] == "uncertain"
    reopened = CloudEditor(
        SyncWorker(CloudCache(editor.cache.path, CREDENTIALS.account_id), cloud)
    )
    assert reopened.reconcile(operation_id)["state"] == "uncertain"
    with pytest.raises(EditingDisabled):
        reopened.edit("Recipe", "create", operation_id, {"title": "Test"})
    reopened.allow_edits = True
    assert (
        reopened.edit("Recipe", "create", operation_id, {"title": "Test"})["state"]
        == "uncertain"
    )
    assert len(cloud.uploads) == 1


def test_uncertain_operation_blocks_new_edits_to_same_entity(
    editor: CloudEditor,
) -> None:
    cloud = editor.worker.session
    cloud.apply_upload = False
    cloud.upload_error = ProbeError("timeout")
    view = editor.view("Recipe", "r1")
    result = editor.edit(
        "Recipe",
        "update",
        str(uuid4()),
        {"notes": "Maybe"},
        entity_id="r1",
        expected_revision=view["revision"],
    )
    assert result["state"] == "uncertain"
    with pytest.raises(EditConflict, match="unresolved"):
        editor.edit(
            "Recipe",
            "delete",
            str(uuid4()),
            {},
            entity_id="r1",
            expected_revision=view["revision"],
        )
    assert len(cloud.uploads) == 1


def test_verification_outage_preserves_uncertain_intent_until_reconcile(
    editor: CloudEditor,
) -> None:
    editor.worker.session.verification_error = True
    result = editor.edit("Recipe", "create", str(uuid4()), {"title": "Test"})
    assert result["state"] == "uncertain"
    editor.worker.session.verification_error = False
    editor.allow_edits = False
    assert editor.reconcile(result["operation_id"])["state"] == "confirmed"
    assert len(editor.worker.session.uploads) == 1


def test_full_snapshot_migration_preserves_photos(tmp_path: Path) -> None:
    path = tmp_path / "cache.db"
    cache = CloudCache(path, CREDENTIALS.account_id)
    cache.apply(
        {"Recipe": {"r1": RECIPE}, "Meal": {}},
        dict.fromkeys(SECTIONS, CURSOR),
        datetime.now(UTC),
    )
    with cache.connect() as conn:
        conn.execute("DROP TABLE RawRecords")
        conn.execute("DROP TABLE EditOperations")
        conn.execute("DELETE FROM Metadata WHERE key='raw_snapshot_ready'")
        conn.execute("PRAGMA user_version=1")
    migrated = CloudCache(path, CREDENTIALS.account_id)
    cloud = FakeCloud()
    SyncWorker(migrated, cloud).sync()
    assert cloud.downloads == [dict.fromkeys(SECTIONS, INITIAL_CURSOR)]
    with migrated.connect() as conn:
        record = json.loads(
            conn.execute("SELECT Record FROM RawRecords WHERE Id='r1'").fetchone()[0]
        )
    assert record["Photo9"] == "ninth-photo"


def test_low_level_upload_gate_is_off_by_default() -> None:
    session = CloudSession(CREDENTIALS)
    session._send = Mock(side_effect=AssertionError("Must not send"))
    with pytest.raises(EditingDisabled):
        session.upload(dict.fromkeys(SECTIONS, CURSOR), "Recipe", {"Id": "r1"})
    session._send.assert_not_called()


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/v1/recipes"),
        ("patch", "/v1/recipes/r1"),
        ("delete", "/v1/recipes/r1"),
        ("post", "/v1/meals"),
        ("patch", "/v1/meals/m1"),
        ("delete", "/v1/meals/m1"),
    ],
)
def test_api_default_off_rejects_before_body_validation(
    editor: CloudEditor, method: str, path: str
) -> None:
    editor.allow_edits = False
    database = CloudDatabase(editor.cache, 300)
    with TestClient(create_app(database, TOKEN, editor=editor)) as client:
        response = client.request(
            method, path, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 403
        assert (
            client.get(
                "/v1/status", headers={"Authorization": f"Bearer {TOKEN}"}
            ).json()["allow_edits"]
            is False
        )
    assert editor.worker.session.uploads == []


def test_api_editing_roundtrip_and_validation(editor: CloudEditor) -> None:
    database = CloudDatabase(editor.cache, 300)
    with TestClient(
        create_app(database, TOKEN, editor=editor),
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        view = client.get("/v1/recipes/r1/editing").json()
        body = {
            "operation_id": str(uuid4()),
            "revision": view["revision"],
            "changes": {"notes": "Changed through API"},
        }
        response = client.patch("/v1/recipes/r1", json=body)
        assert response.status_code == 200
        assert response.json()["state"] == "confirmed"
        assert client.get("/v1/recipes/r1").json()["notes"] == "Changed through API"
        assert client.get("/v1/status").json()["allow_edits"] is True
        assert (
            client.get("/v1/edit-operations/" + body["operation_id"]).json()["state"]
            == "confirmed"
        )
        assert (
            client.patch(
                "/v1/recipes/r1", json={**body, "changes": {"Photo": "bad"}}
            ).status_code
            == 422
        )
        assert (
            client.patch(
                "/v1/recipes/r1", json={**body, "changes": {"notes": None}}
            ).status_code
            == 422
        )
        assert (
            client.patch(
                "/v1/recipes/r1", json={**body, "operation_id": str(uuid4())}
            ).status_code
            == 409
        )


def test_api_auth_required_even_with_editing_enabled(editor: CloudEditor) -> None:
    with TestClient(
        create_app(CloudDatabase(editor.cache, 300), TOKEN, editor=editor)
    ) as client:
        assert client.post("/v1/recipes").status_code == 401
    assert editor.worker.session.uploads == []


def test_revision_detects_unknown_field_changes() -> None:
    assert revision({"Photo": "a", "LastModified": CURSOR}) != revision(
        {"Photo": "b", "LastModified": CURSOR}
    )


@pytest.mark.parametrize("part", [None, [], "invalid", {}, {"UpdatedLocalItems": []}])
@pytest.mark.parametrize("applied,state", [(True, "confirmed"), (False, "uncertain")])
def test_malformed_upload_reply_is_reconciled_without_replay(
    editor: CloudEditor, part: object, applied: bool, state: str
) -> None:
    cloud = editor.worker.session
    cloud.malformed_part = True
    cloud.upload_part = part
    cloud.apply_upload = applied
    operation_id = str(uuid4())
    result = editor.edit("Recipe", "create", operation_id, {"title": "Test"})
    assert result["state"] == state
    assert (
        editor.edit("Recipe", "create", operation_id, {"title": "Test"})["state"]
        == state
    )
    assert len(cloud.uploads) == 1


def test_equivalent_utc_added_date_is_confirmed(editor: CloudEditor) -> None:
    cloud = editor.worker.session
    original_upload = cloud.upload

    def normalize(cursors: dict, section: str, record: dict) -> dict:
        result = original_upload(cursors, section, record)
        cloud.records[section][record["Id"]]["AddedDate"] = record["AddedDate"].replace(
            "+00:00", "Z"
        )
        return result

    cloud.upload = normalize
    result = editor.edit("Recipe", "create", str(uuid4()), {"title": "Test"})
    assert result["state"] == "confirmed"


@pytest.mark.parametrize("applied,state", [(True, "confirmed"), (False, "uncertain")])
def test_interrupted_http_response_is_reconciled(
    editor: CloudEditor, monkeypatch: pytest.MonkeyPatch, applied: bool, state: str
) -> None:
    cloud = editor.worker.session
    cloud.apply_upload = applied
    original = cloud.upload
    response = Mock()
    response.read.side_effect = IncompleteRead(b"synthetic partial response", 100)
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = context
    monkeypatch.setattr(cloud_probe, "build_opener", Mock(return_value=opener))
    transport = CloudSession(CREDENTIALS, allow_edits=True)
    transport._token = "synthetic-token"

    def interrupted(cursors: dict, section: str, record: dict) -> dict:
        original(cursors, section, record)
        return transport.upload(cursors, section, record)

    cloud.upload = interrupted
    operation_id = str(uuid4())
    result = editor.edit("Recipe", "create", operation_id, {"title": "Test"})
    assert result["state"] == state
    assert len(cloud.uploads) == 1
    assert opener.open.call_count == 1


def test_cloud_session_retains_full_photo_data() -> None:
    session = CloudSession(CREDENTIALS)
    session._token = "synthetic-token"
    session._send = Mock(
        return_value={
            "RecipeSyncEntityResponse": {
                "UpdatedLocalItems": [],
                "UpdatedServerItems": [
                    {
                        "Id": "r1",
                        "Photo": "keep",
                        "Photo9": "keep-nine",
                        "LastModified": CURSOR,
                    }
                ],
                "MaxLastModified": CURSOR,
                "HasMoreUpdatedServerItems": False,
            }
        }
    )
    page = session.page({"Recipe": INITIAL_CURSOR})["Recipe"]
    assert page.entities[0]["Photo"] == "keep"
    assert page.entities[0]["Photo9"] == "keep-nine"


@pytest.mark.parametrize(
    "resource,changes",
    [
        ("recipes", {"title": "API test recipe"}),
        ("meals", {"title": "API test meal", "date": "2026-11-20"}),
    ],
)
def test_api_create_update_delete_lifecycle(
    editor: CloudEditor, resource: str, changes: dict
) -> None:
    with TestClient(
        create_app(CloudDatabase(editor.cache, 300), TOKEN, editor=editor),
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        created = client.post(
            f"/v1/{resource}", json={"operation_id": str(uuid4()), "changes": changes}
        )
        assert created.status_code == 200
        entity_id = created.json()["entity_id"]
        path = f"/v1/{resource}/{entity_id}"
        view = client.get(path + "/editing").json()
        assert view["fields"]["title"] == changes["title"]
        updated = client.patch(
            path,
            json={
                "operation_id": str(uuid4()),
                "revision": view["revision"],
                "changes": {"notes": "API updated"},
            },
        )
        assert updated.status_code == 200
        view = client.get(path + "/editing").json()
        assert view["fields"]["notes"] == "API updated"
        deleted = client.request(
            "DELETE",
            path,
            json={"operation_id": str(uuid4()), "revision": view["revision"]},
        )
        assert deleted.status_code == 200
        assert client.get(path + "/editing").status_code == 404


def test_http_failure_is_sanitized_and_not_replayed(editor: CloudEditor) -> None:
    cloud = editor.worker.session
    cloud.apply_upload = False
    cloud.upload_error = CloudHTTPError(503)
    result = editor.edit("Recipe", "create", str(uuid4()), {"title": "Test"})
    assert result["state"] == "uncertain"
    assert result["error"] == "upload_http_503"
    assert len(cloud.uploads) == 1
