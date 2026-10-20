"""Import durability, explicit selection, and safe recovery through HA actions."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.recipe_keeper.shopping_import import ShoppingImporter

START = "2026-10-20"
END = "2026-10-26"


async def prepare(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> dict:
    client.sync.return_value["last_successful_sync_at"] = datetime.now(UTC).isoformat()
    client.preview.return_value = {
        "issues": [],
        "selected_items": [
            {"key": "k1", "text": "2 carrots"},
            {"key": "k2", "text": "1 onion"},
        ],
    }
    hass.states.async_set("todo.test", "0", {"supported_features": 1})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return {
        "config_entry_id": entry.entry_id,
        "start_date": START,
        "end_date": END,
        "selected_keys": ["k1", "k2"],
        "todo_entity": "todo.test",
        "import_id": str(uuid4()),
    }


async def invoke(hass: HomeAssistant, data: dict) -> dict:
    return await hass.services.async_call(
        "recipe_keeper", "import_ingredients", data, blocking=True, return_response=True
    )


async def test_explicit_selection_and_replay(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    data = await prepare(hass, entry, client)
    add = AsyncMock()
    hass.services.async_register("todo", "add_item", add)
    result = await invoke(hass, data)
    assert result["state"] == "complete"
    assert result["added_keys"] == ["k1", "k2"]
    assert result["not_attempted_keys"] == []
    assert add.await_count == 2
    await invoke(hass, data)
    assert add.await_count == 2
    data["import_id"] = str(uuid4())
    result = await invoke(hass, data)
    assert result["skipped_keys"] == ["k1", "k2"]
    assert add.await_count == 2
    assert client.preview.call_args.args[-1] == ["k1", "k2"]
    assert client.preview.call_args.args[-2] == ["olive oil"]


@pytest.mark.parametrize(
    (
        "failure_save",
        "before_count",
        "after_count",
        "state",
        "uncertain",
        "untouched",
        "added",
    ),
    [
        pytest.param(1, 0, 2, "complete", [], [], ["k1", "k2"], id="first-save"),
        pytest.param(2, 0, 0, "interrupted", [], ["k1", "k2"], [], id="before-add"),
        pytest.param(3, 1, 1, "uncertain", ["k1"], ["k2"], [], id="after-add"),
    ],
)
async def test_disk_failure_never_loses_pending_add(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: AsyncMock,
    failure_save: int,
    before_count: int,
    after_count: int,
    state: str,
    uncertain: list[str],
    untouched: list[str],
    added: list[str],
) -> None:
    data = await prepare(hass, entry, client)
    add = AsyncMock()
    hass.services.async_register("todo", "add_item", add)
    store = entry.runtime_data.importer.store
    original = store._save
    count = 0

    def write(record: dict) -> None:
        nonlocal count
        count += 1
        if count == failure_save:
            raise WriteError("Synthetic disk full")
        original(record)

    with (
        patch.object(store, "_save", side_effect=write),
        pytest.raises(HomeAssistantError),
    ):
        await invoke(hass, data)
    assert add.await_count == before_count
    entry.runtime_data.importer = ShoppingImporter(hass, entry.entry_id)
    result = await invoke(hass, data)
    assert result["state"] == state
    assert result["added_keys"] == added
    assert result["uncertain_keys"] == uncertain
    assert result["not_attempted_keys"] == untouched
    assert add.await_count == after_count


async def test_unknown_add_and_restart_preserve_remainder(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    data = await prepare(hass, entry, client)
    add = AsyncMock(side_effect=HomeAssistantError("Synthetic lost response"))
    hass.services.async_register("todo", "add_item", add)
    result = await invoke(hass, data)
    assert result["state"] == "uncertain"
    assert result["uncertain_keys"] == ["k1"]
    assert result["not_attempted_keys"] == ["k2"]
    entry.runtime_data.importer = ShoppingImporter(hass, entry.entry_id)
    assert (await invoke(hass, data))["uncertain_keys"] == ["k1"]
    assert add.await_count == 1


async def test_unload_waits_and_stops_remaining_adds(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    data = await prepare(hass, entry, client)
    started, release = asyncio.Event(), asyncio.Event()

    async def add(call) -> None:
        started.set()
        await release.wait()

    handler = AsyncMock(side_effect=add)
    hass.services.async_register("todo", "add_item", handler)
    importing = asyncio.create_task(invoke(hass, data))
    await started.wait()
    closing = asyncio.Event()

    async def closed() -> None:
        closing.set()

    client.close.side_effect = closed
    unloading = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
    await closing.wait()
    assert entry.runtime_data.importer.closed
    assert not unloading.done()
    release.set()
    assert (await importing)["state"] == "interrupted"
    assert await unloading
    assert handler.await_count == 1
    assert await hass.config_entries.async_setup(entry.entry_id)
    data["import_id"] = str(uuid4())
    await invoke(hass, data)
    assert handler.await_count == 2
    assert [call.args[0].data["item"] for call in handler.await_args_list] == [
        "2 carrots",
        "1 onion",
    ]


async def test_corrupt_ledger_and_stopping_fail_closed(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    data = await prepare(hass, entry, client)
    add = AsyncMock()
    hass.services.async_register("todo", "add_item", add)
    path = entry.runtime_data.importer.store.path
    await hass.async_add_executor_job(path.parent.mkdir, 0o700, True, True)
    await hass.async_add_executor_job(path.write_text, "broken-json")
    with pytest.raises(HomeAssistantError, match="ledger unavailable"):
        await invoke(hass, data)
    hass.set_state(CoreState.stopping)
    with pytest.raises(HomeAssistantError, match="stopping"):
        await invoke(hass, data)
    add.assert_not_awaited()


async def test_cancelled_add_is_never_replayed(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> None:
    from datetime import date
    from types import SimpleNamespace

    from homeassistant.core import Context

    data = await prepare(hass, entry, client)
    began = asyncio.Event()

    async def add(call) -> None:
        began.set()
        await asyncio.Event().wait()

    handler = AsyncMock(side_effect=add)
    hass.services.async_register("todo", "add_item", handler)
    importer = entry.runtime_data.importer
    task = asyncio.create_task(
        importer.import_items(
            entry.runtime_data,
            SimpleNamespace(context=Context()),
            date(2026, 10, 20),
            date(2026, 10, 26),
            data["selected_keys"],
            "todo.test",
            data["import_id"],
            False,
        )
    )
    await began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    entry.runtime_data.importer = ShoppingImporter(hass, entry.entry_id)
    result = await invoke(hass, data)
    assert result["uncertain_keys"] == ["k1"]
    assert result["not_attempted_keys"] == ["k2"]
    assert handler.await_count == 1
