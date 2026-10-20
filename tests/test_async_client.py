"""Native aiohttp bridge uses the same engine without blocking HA's loop."""

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from recipe_assistant.async_client import AiohttpTransport, AsyncRecipeClient
from recipe_assistant.cloud_edits import EditingDisabled
from recipe_assistant.cloud_protocol import (
    AuthenticationError,
    CloudHTTPError,
    ProbeError,
)

from .test_cloud_backend import CREDENTIALS


class Response:
    def __init__(
        self, data: dict, status: int = 200, headers: dict | None = None
    ) -> None:
        self.raw = json.dumps(data).encode()
        self.status = status
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size: int):
        yield self.raw


@pytest.mark.asyncio
async def test_aiohttp_login_renewal_and_readonly() -> None:
    session = Mock()
    empty = {
        kind + "SyncEntityResponse": {
            "UpdatedServerItems": [],
            "UpdatedLocalItems": [],
            "MaxLastModified": "2026-10-20T00:00:00Z",
            "HasMoreUpdatedServerItems": False,
        }
        for kind in ("Recipe", "Meal")
    }
    session.post.side_effect = [
        Response({"access_token": "first"}),
        Response({}, 401),
        Response({"access_token": "second"}),
        Response(empty),
    ]
    transport = AiohttpTransport(session, CREDENTIALS)
    result = await asyncio.to_thread(
        transport.page,
        {"Recipe": "1601-01-01T00:00:00Z", "Meal": "1601-01-01T00:00:00Z"},
    )
    assert result["Recipe"].entities == []
    assert session.post.call_count == 4
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer second"
    assert session.post.call_args.kwargs["allow_redirects"] is False
    with pytest.raises(EditingDisabled, match="disabled"):
        await asyncio.to_thread(transport.upload, {}, "Recipe", {})
    with pytest.raises(RuntimeError, match="executor"):
        transport._login()
    transport.close()
    with pytest.raises(ProbeError, match="closed"):
        await asyncio.to_thread(transport._login)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, AuthenticationError),
        (401, AuthenticationError),
        (429, CloudHTTPError),
        (302, CloudHTTPError),
    ],
)
async def test_auth_errors_sanitized(status: int, expected: type[Exception]) -> None:
    session = Mock()
    session.post.return_value = Response(
        {"secret": "not in errors"}, status, {"Retry-After": "60"}
    )
    transport = AiohttpTransport(session, CREDENTIALS)
    with pytest.raises(expected) as error:
        await asyncio.to_thread(transport._login)
    assert "not in errors" not in str(error.value)
    transport.close()


@pytest.mark.asyncio
async def test_cancelled_caller_holds_executor_lock(tmp_path: Path) -> None:
    client = AsyncRecipeClient(Mock(), CREDENTIALS, tmp_path / "cache.db")
    began, release = threading.Event(), threading.Event()
    order = []

    def first() -> None:
        began.set()
        release.wait(5)
        order.append("first")

    task = asyncio.create_task(client._run(first))
    await asyncio.to_thread(began.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    second = asyncio.create_task(client._run(order.append, "second"))
    await asyncio.sleep(0)
    assert order == []
    release.set()
    await second
    assert order == ["first", "second"]
    await client.close()


@pytest.mark.asyncio
async def test_close_interrupts_http_and_drains_worker(tmp_path: Path) -> None:
    client = AsyncRecipeClient(Mock(), CREDENTIALS, tmp_path / "cache.db")
    started = asyncio.Event()

    async def never(*args) -> bytes:
        started.set()
        await asyncio.Event().wait()
        return b""

    client.transport._post = never
    login = asyncio.create_task(client.authenticate())
    await started.wait()
    await client.close()
    with pytest.raises(ProbeError, match="interrupted"):
        await login
    assert not client._jobs


@pytest.mark.asyncio
async def test_disable_during_401_login_keeps_sent_evidence() -> None:
    session = Mock()
    transport = AiohttpTransport(session, CREDENTIALS, allow_edits=True)
    transport._token = "first"
    session.post.side_effect = [Response({}, 401), Response({"access_token": "second"})]
    original = transport._login

    def login() -> str:
        value = original()
        transport.allow_edits = False
        return value

    transport._login = login
    with pytest.raises(EditingDisabled) as error:
        await asyncio.to_thread(
            transport.upload,
            {"Recipe": "1601-01-01T00:00:00Z", "Meal": "1601-01-01T00:00:00Z"},
            "Recipe",
            {"Id": "synthetic"},
        )
    assert error.value.request_started is True
    assert session.post.call_count == 2
    transport.close()
