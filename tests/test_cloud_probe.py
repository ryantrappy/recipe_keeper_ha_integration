"""Cloud protocol tests never contact Recipe Keeper or read real credentials."""

import hashlib
import plistlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from recipe_assistant.cloud_probe import (
    INITIAL_CURSOR,
    CloudProbe,
    NoRedirects,
    ProbeError,
    compare_local,
    read_mac_session,
    request_body,
    summarize,
)
from recipe_assistant.database import Database

CURSOR = "2026-10-19T21:01:20.9752375Z"


def response_section(
    rows: list[dict],
    cursor: str = CURSOR,
    more: bool = False,
) -> dict:
    return {
        "UpdatedLocalItems": [],
        "UpdatedServerItems": rows,
        "MaxLastModified": cursor,
        "HasMoreUpdatedServerItems": more,
    }


def test_requests_never_upload() -> None:
    assert request_body({"Recipe": CURSOR, "Meal": INITIAL_CURSOR}) == {
        "RecipeSyncEntityRequest": {
            "MaxLastModified": CURSOR,
            "SyncEntities": [],
            "LocalChangesIds": [],
        },
        "MealSyncEntityRequest": {
            "MaxLastModified": INITIAL_CURSOR,
            "SyncEntities": [],
            "LocalChangesIds": [],
        },
    }


@pytest.mark.parametrize("cursors", [{}, {"Account": CURSOR}, {"ShoppingList": CURSOR}])
def test_probe_restricts_entity_types(cursors: dict[str, str]) -> None:
    with pytest.raises(ProbeError, match="Only recipe and meal"):
        request_body(cursors)


def test_pagination_retains_100ns_precision_and_completed_cursor() -> None:
    probe = CloudProbe("synthetic-token")
    probe._send = Mock(
        side_effect=[
            {
                "RecipeSyncEntityResponse": response_section(
                    [{"Id": "r1", "Photo": "private-photo", "Ingredients": "1 lemon"}],
                    more=True,
                ),
                "MealSyncEntityResponse": response_section([{"Id": "m1"}]),
            },
            {
                "RecipeSyncEntityResponse": response_section(
                    [{"Id": "deleted", "IsDeleted": True}],
                    cursor="2026-10-19T22:00:00Z",
                )
            },
        ]
    )
    data, cursors = probe.download()
    second_request = probe._send.call_args_list[1].args[0]
    assert second_request == request_body({"Recipe": CURSOR})
    assert "Photo" not in data["Recipe"]["r1"]
    assert data["Recipe"]["r1"]["Ingredients"] == "1 lemon"
    assert data["Recipe"]["deleted"]["IsDeleted"] is True
    assert cursors == {"Recipe": "2026-10-19T22:00:00Z", "Meal": CURSOR}


@pytest.mark.parametrize(
    "part",
    [
        None,
        {"UpdatedLocalItems": [{}]},
        response_section([{"Id": 42}]),
        response_section([], cursor=""),
        response_section([], cursor=INITIAL_CURSOR, more=True),
    ],
)
def test_bad_responses_stop(part: dict | None) -> None:
    probe = CloudProbe("synthetic-token")
    probe._send = Mock(return_value={"RecipeSyncEntityResponse": part})
    with pytest.raises(ProbeError):
        probe.page({"Recipe": INITIAL_CURSOR})


def test_page_limit_does_not_return_incomplete_data() -> None:
    probe = CloudProbe("synthetic-token")
    probe._send = Mock(
        return_value={
            "RecipeSyncEntityResponse": response_section([], more=True),
            "MealSyncEntityResponse": response_section([]),
        }
    )
    with pytest.raises(ProbeError, match="page limit"):
        probe.download(max_pages=1)


def test_compare_local_reports_counts_without_recipe_content(
    database: Database,
) -> None:
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    data = {
        "Recipe": {
            "r1": {"Id": "r1", "Title": "Lemon Soup", "Ingredients": "different"},
            "cloud-only": {"Id": "cloud-only", "Title": "Secret family recipe"},
            "deleted": {"Id": "deleted", "IsDeleted": True},
        },
        "Meal": {},
    }
    result = compare_local(data, database.path)
    assert result["Recipe"] == {
        "local_active": 2,
        "cloud_active": 2,
        "only_local": 1,
        "only_cloud": 1,
        "text_mismatches": 1,
    }
    assert "Secret family recipe" not in str(result)
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before


def test_mac_session_reads_token_without_changing_preferences(tmp_path: Path) -> None:
    path = tmp_path / "preferences.plist"
    original = plistlib.dumps({"AccountBearerToken": "synthetic-token"})
    path.write_bytes(original)
    assert read_mac_session(path) == "synthetic-token"
    assert path.read_bytes() == original


def test_missing_mac_token(tmp_path: Path) -> None:
    path = tmp_path / "preferences.plist"
    path.write_bytes(plistlib.dumps({}))
    with pytest.raises(ProbeError, match="No cached bearer token"):
        read_mac_session(path)


def test_redirects_are_refused() -> None:
    assert (
        NoRedirects().redirect_request(None, None, 302, "", {}, "https://example.com")
        is None
    )


def test_summary_does_not_contain_token_or_recipe_text() -> None:
    probe = CloudProbe("synthetic-secret-token")
    probe._send = Mock(
        side_effect=[
            {
                "RecipeSyncEntityResponse": response_section(
                    [{"Id": "r1", "Title": "Private recipe", "IsDeleted": False}],
                ),
                "MealSyncEntityResponse": response_section([]),
            },
            {
                "RecipeSyncEntityResponse": response_section([]),
                "MealSyncEntityResponse": response_section([]),
            },
        ]
    )
    result = summarize(probe)
    assert result["uploaded_entities"] == 0
    assert result["download"]["Recipe"]["active_entities"] == 1
    assert "Private recipe" not in str(result)
    assert "synthetic-secret-token" not in str(result)
