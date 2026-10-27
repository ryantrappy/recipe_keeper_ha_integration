"""Automation intents survive retries without changing their selected contents."""

import copy
from unittest.mock import Mock
from uuid import uuid4

import pytest

from recipe_assistant.automation import AutomationActions
from recipe_assistant.cloud_backend import CloudDatabase
from recipe_assistant.cloud_edits import CloudEditor, EditConflict, EditingDisabled

from .test_cloud_edits import editor as editor


def planner(editor: CloudEditor) -> AutomationActions:
    return AutomationActions(editor, CloudDatabase(editor.cache, 300))


def intent(**changes) -> dict:
    return {
        "action": "set_mealplan",
        "date": "2026-10-28",
        "entry_type": "dinner",
        "recipe_id": "r1",
        "note_title": None,
        "note_text": None,
        **changes,
    }


def test_plan_by_id_and_retry_after_recipe_deleted(editor: CloudEditor) -> None:
    actions = planner(editor)
    operation_id = str(uuid4())
    original = copy.deepcopy(editor.worker.session.records["Recipe"]["r1"])
    result = actions.plan(operation_id, intent())
    assert result["state"] == "confirmed"
    assert result["recipe_id"] == "r1"
    assert result["mealplan_id"] == result["entity_id"]
    assert result["mealplan"]["title"] == original["Title"]
    assert len(result["mealplan"]["revision"]) == 64
    section, record = editor.worker.session.uploads[0]
    assert section == "Meal"
    assert record["Title"] == original["Title"]
    assert record["MealDate"] == "2026-10-28T00:00:00"
    del editor.worker.session.records["Recipe"]["r1"]
    assert planner(editor).plan(operation_id, intent()) == result
    assert len(editor.worker.session.uploads) == 1
    with pytest.raises(EditConflict, match="different request"):
        actions.plan(operation_id, intent(date="2026-10-29"))


def test_plan_note_and_preserve_unedited_fields(editor: CloudEditor) -> None:
    actions = planner(editor)
    view = editor.view("Meal", "m1")
    result = actions.plan(
        str(uuid4()),
        intent(
            action="update_mealplan",
            mealplan_id="m1",
            revision=view["revision"],
            recipe_id=None,
            note_title="Eat out",
            note_text="Meet friends",
            entry_type="lunch",
        ),
    )
    assert result["state"] == "confirmed"
    updated = editor.worker.session.records["Meal"]["m1"]
    assert updated["Title"] == "Eat out"
    assert updated["Notes"] == "Meet friends"
    assert updated["MealTypeName"] == "Lunch"
    assert updated["MealType"] == 3
    assert updated["Colour"] == 42
    assert editor.worker.session.records["Recipe"]["r1"]["UnknownField"] == {
        "nested": [1, 2, 3]
    }


def test_random_choice_saved_before_upload_and_not_rechosen(
    editor: CloudEditor, monkeypatch: pytest.MonkeyPatch
) -> None:
    actions = planner(editor)
    chooser = Mock(side_effect=lambda candidates: candidates[0])
    monkeypatch.setattr("recipe_assistant.automation.secrets.choice", chooser)
    operation_id = str(uuid4())
    request = intent(
        action="set_random_mealplan", search_terms="", favourite_only=False
    )
    editor.worker.session.apply_upload = False
    result = actions.plan(operation_id, request)
    assert result["state"] == "uncertain"
    assert planner(editor).plan(operation_id, request) == result
    chooser.assert_called_once()
    assert len(editor.worker.session.uploads) == 1
    with editor.cache.connect() as conn:
        assert (
            conn.execute("SELECT count(*) FROM AutomationRequests").fetchone()[0] == 1
        )


@pytest.mark.parametrize("random", [False, True])
def test_duplicate_titles_are_not_plannable(editor: CloudEditor, random: bool) -> None:
    duplicate = copy.deepcopy(editor.worker.session.records["Recipe"]["r1"])
    duplicate["Id"] = "r2"
    editor.worker.session.records["Recipe"]["r2"] = duplicate
    request = intent()
    if random:
        request = intent(
            action="set_random_mealplan", search_terms="", favourite_only=False
        )
    with pytest.raises(ValueError, match="ambiguous|uniquely titled"):
        planner(editor).plan(str(uuid4()), request)
    assert editor.worker.session.uploads == []


def test_random_filter_excludes_non_favourites(editor: CloudEditor) -> None:
    editor.worker.session.records["Recipe"]["r1"]["IsFavourite"] = False
    with pytest.raises(ValueError, match="No uniquely titled"):
        planner(editor).plan(
            str(uuid4()),
            intent(action="set_random_mealplan", search_terms="", favourite_only=True),
        )
    assert editor.worker.session.uploads == []


def test_import_intent_keeps_original_page_contents(editor: CloudEditor) -> None:
    actions = planner(editor)
    request = {
        "action": "import_recipe",
        "url": "https://example.com/soup",
        "include_tags": False,
        "recipe_index": None,
    }
    operation_id = str(uuid4())
    result = actions.import_recipe(
        operation_id,
        request,
        {"title": "Imported soup", "ingredients": "2 carrots", "directions": "Boil"},
    )
    assert result["state"] == "confirmed"
    assert result["recipe"]["ingredients"] == "2 carrots"
    assert (
        actions.import_recipe(operation_id, request, {"title": "Changed website"})
        == result
    )
    assert len(editor.worker.session.uploads) == 1
    assert editor.worker.session.uploads[0][1]["Title"] == "Imported soup"
    with pytest.raises(EditConflict):
        actions.import_recipe(
            operation_id, {**request, "include_tags": True}, {"title": "Other"}
        )


def test_edit_gate_and_stale_revision(editor: CloudEditor) -> None:
    editor.allow_edits = False
    with pytest.raises(EditingDisabled):
        planner(editor).plan(str(uuid4()), intent())
    editor.allow_edits = True
    with pytest.raises(EditConflict, match="Record changed"):
        planner(editor).plan(
            str(uuid4()),
            intent(action="update_mealplan", mealplan_id="m1", revision="a" * 64),
        )
    assert editor.worker.session.uploads == []


@pytest.mark.parametrize(
    ("new_title", "other_title"),
    [
        pytest.param("Renamed recipe", "Different recipe", id="renamed"),
        pytest.param("Renamed recipe", None, id="title-reassigned"),
    ],
)
def test_final_refresh_revalidates_frozen_recipe_link(
    editor: CloudEditor,
    monkeypatch: pytest.MonkeyPatch,
    new_title: str,
    other_title: str | None,
) -> None:
    cloud = editor.worker.session
    original = editor.worker.sync_locked
    old_title = cloud.records["Recipe"]["r1"]["Title"]

    def change_title() -> None:
        cloud.records["Recipe"]["r1"]["Title"] = new_title
        cloud.records["Recipe"]["r2"] = {
            **copy.deepcopy(cloud.records["Recipe"]["r1"]),
            "Id": "r2",
            "Title": other_title or old_title,
        }

    changes = iter((lambda: None, change_title))

    def sync(**kwargs) -> None:
        next(changes)()
        original(**kwargs)

    monkeypatch.setattr(editor.worker, "sync_locked", sync)
    with pytest.raises(EditConflict, match="no meal was uploaded"):
        planner(editor).plan(str(uuid4()), intent())
    assert cloud.uploads == []
    assert editor.counts() == {}


def test_crash_after_choice_before_editor_rechecks_link_on_resume(
    editor: CloudEditor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = planner(editor)
    operation_id = str(uuid4())
    monkeypatch.setattr(
        actions, "execute", Mock(side_effect=RuntimeError("Synthetic crash"))
    )
    with pytest.raises(RuntimeError, match="Synthetic crash"):
        actions.plan(operation_id, intent())
    assert editor.worker.session.uploads == []
    assert editor.operation(operation_id) is None
    editor.worker.session.records["Recipe"]["r1"]["Title"] = "Renamed while offline"
    with pytest.raises(EditConflict, match="no meal was uploaded"):
        planner(editor).plan(operation_id, intent())
    assert editor.worker.session.uploads == []
