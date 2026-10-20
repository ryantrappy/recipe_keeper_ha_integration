"""Prove the opt-in harness cannot change pre-existing or unowned records."""

from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from scripts.validate_cloud_edits import OwnedSession

from .test_cloud_backend import CREDENTIALS, CURSOR


@pytest.mark.parametrize(
    "record,original_ids",
    [
        pytest.param(
            {"Id": "original", "LastModified": None}, {"original"}, id="original-id"
        ),
        pytest.param(
            {"Id": "unowned", "LastModified": CURSOR}, set(), id="unowned-update"
        ),
        pytest.param(
            {"Id": "unowned", "LastModified": None, "IsDeleted": True},
            set(),
            id="unowned-delete",
        ),
    ],
)
def test_harness_refuses_protected_ids(
    tmp_path: Path, record: dict, original_ids: set[str]
) -> None:
    session = OwnedSession(CREDENTIALS, tmp_path, "synthetic-run")
    session.original_ids = original_ids
    session._send = Mock(side_effect=AssertionError("Must not send"))
    record["Title"] = "Recipe Assistant validation synthetic-run Recipe"
    with pytest.raises(RuntimeError):
        session.upload({"Recipe": CURSOR}, "Recipe", record)
    session._send.assert_not_called()


def test_harness_allows_only_its_own_created_record_lifecycle(tmp_path: Path) -> None:
    run_id = str(uuid4())
    session = OwnedSession(CREDENTIALS, tmp_path, run_id)
    session._token = "synthetic-token"
    session._send = Mock(return_value={})
    entity_id = str(uuid4())
    created = {
        "Id": entity_id,
        "Title": f"Recipe Assistant validation {run_id} Recipe",
        "LastModified": None,
        "IsDeleted": False,
    }
    session.upload({"Recipe": CURSOR}, "Recipe", created)
    saved = tmp_path / f"owned-{entity_id}.json"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert session.owned == {entity_id: "Recipe"}
    updated = {**created, "LastModified": CURSOR, "Notes": "Edited disposable"}
    session.upload({"Recipe": CURSOR}, "Recipe", updated)
    session.upload({"Recipe": CURSOR}, "Recipe", {**updated, "IsDeleted": True})
    assert session._send.call_count == 3
    with pytest.raises(RuntimeError):
        session.upload(
            {"Recipe": CURSOR}, "Recipe", {**updated, "Title": "Real recipe title"}
        )
    with pytest.raises(RuntimeError):
        session.upload(
            {"Meal": CURSOR},
            "Meal",
            {**updated, "Title": f"Recipe Assistant validation {run_id} Meal"},
        )
    assert session._send.call_count == 3
