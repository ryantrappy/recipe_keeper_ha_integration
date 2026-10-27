"""Aiohttp transport and async facade for the shared persistent sync engine."""

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError, Future
from functools import partial
from pathlib import Path
from urllib.parse import urlencode

import aiohttp

from .automation import AutomationActions
from .cloud_backend import (
    CloudCache,
    CloudDatabase,
    CloudSession,
    Credentials,
    SyncWorker,
)
from .cloud_edits import CloudEditor
from .cloud_protocol import (
    LOGIN_URL,
    SYNC_URL,
    AuthenticationError,
    CloudHTTPError,
    ProbeError,
    parse_authentication,
)
from .recipe_import import fetch_recipe_page, parse_recipe_page, validate_url
from .shopping import select_ingredients
from .sync_snapshot import read_sync_snapshot


class AiohttpTransport(CloudSession):
    """Run sync-engine calls on a worker and HTTP on the owning event loop."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: Credentials,
        *,
        allow_edits: bool = False,
    ) -> None:
        super().__init__(credentials, allow_edits=allow_edits)
        self.session = session
        self.loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._closed = False
        self._guard = threading.Lock()
        self._pending: set[Future] = set()

    async def _post(self, url: str, data: bytes, headers: dict, limit: int) -> bytes:
        try:
            async with self.session.post(
                url,
                data=data,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                if response.status != 200:
                    retry = response.headers.get("Retry-After", "")
                    raise CloudHTTPError(
                        response.status,
                        int(retry) if retry.isascii() and retry.isdecimal() else 0,
                    )
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise ProbeError("Cloud response size limit exceeded")
                return bytes(raw)
        except (aiohttp.ClientError, TimeoutError):
            raise ProbeError("Cloud connection failed; response omitted") from None

    def _request(self, url: str, data: bytes, headers: dict, limit: int) -> bytes:
        if threading.get_ident() == self._loop_thread_id:
            raise RuntimeError("Cloud transport must run in an executor")
        with self._guard:
            if self._closed:
                raise ProbeError("Cloud client is closed")
            future = asyncio.run_coroutine_threadsafe(
                self._post(url, data, headers, limit), self.loop
            )
            self._pending.add(future)
        try:
            return future.result(timeout=55)
        except (CancelledError, TimeoutError):
            future.cancel()
            raise ProbeError(
                "Cloud request interrupted; outcome may be unknown"
            ) from None
        finally:
            with self._guard:
                self._pending.discard(future)

    def _login(self) -> str:
        try:
            raw = self._request(
                LOGIN_URL,
                urlencode(
                    {
                        "username": self.credentials.email,
                        "password": self.credentials.password,
                        "grant_type": "password",
                    }
                ).encode(),
                {
                    "X-RK-APIVersion": "10",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                1_000_000,
            )
        except CloudHTTPError as exc:
            if exc.status_code in (400, 401):
                raise AuthenticationError(
                    "Recipe Keeper credentials rejected"
                ) from None
            raise
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ProbeError("Invalid authentication response") from None
        return parse_authentication(data).token

    def _send(self, body: dict) -> dict:
        raw = self._request(
            SYNC_URL,
            json.dumps(body).encode(),
            {
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "X-RK-APIVersion": "10",
                "User-Agent": "recipe-keeper-client/0.2",
            },
            50_000_000,
        )
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ProbeError("Invalid cloud response") from None
        if not isinstance(result, dict):
            raise ProbeError("Unexpected cloud response format")
        return result

    def close(self) -> None:
        with self._guard:
            self.allow_edits = False
            self._closed = True
            for future in self._pending:
                future.cancel()


class AsyncRecipeClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        credentials: Credentials,
        cache_path: Path,
        *,
        allow_edits: bool = False,
        executor: Callable[..., Awaitable] = asyncio.to_thread,
    ) -> None:
        self.credentials = credentials
        self.cache_path = cache_path
        self.allow_edits = allow_edits
        self.transport = AiohttpTransport(session, credentials, allow_edits=allow_edits)
        self.executor = executor
        self._lock = asyncio.Lock()
        self._closed = False
        self._jobs: set[asyncio.Future] = set()
        self.worker: SyncWorker | None = None
        self.database: CloudDatabase | None = None
        self.editor: CloudEditor | None = None
        self.automations: AutomationActions | None = None

    async def _run(self, function: Callable, *args, **kwargs):
        await self._lock.acquire()
        if self._closed:
            self._lock.release()
            raise ProbeError("Cloud client is closed")
        try:
            job = asyncio.ensure_future(
                self.executor(partial(function, *args, **kwargs))
            )
        except BaseException:
            self._lock.release()
            raise
        self._jobs.add(job)

        def finished(future: asyncio.Future) -> None:
            self._jobs.discard(future)
            self._lock.release()
            if not future.cancelled():
                future.exception()

        job.add_done_callback(finished)
        return await asyncio.shield(job)

    async def authenticate(self) -> str:
        self.transport._token = await self._run(self.transport._login)
        return self.credentials.account_id

    async def initialize(self) -> None:
        def setup() -> None:
            cache = CloudCache(self.cache_path, self.credentials.account_id)
            self.worker = SyncWorker(cache, self.transport, 300)
            self.database = CloudDatabase(cache, 300)
            self.editor = CloudEditor(self.worker, allow_edits=self.allow_edits)
            self.automations = AutomationActions(self.editor, self.database)

        await self._run(setup)

    async def sync(self) -> dict:
        def refresh() -> dict:
            self.worker.sync()
            return self._status()

        return await self._run(refresh)

    def _status(self) -> dict:
        state = self.database.status()
        state.allow_edits = self.editor.enabled
        state.edit_operations = self.editor.counts()
        return {
            **state.model_dump(mode="json"),
            "calendar_meals": self.database.calendar_meals()
            if state.cache_ready
            else [],
        }

    async def cached_status(self) -> dict:
        return await self._run(self._status)

    async def search(self, query: str = "", limit: int = 25, offset: int = 0) -> dict:
        return await self._run(
            lambda: self.database.search(query, limit, offset).model_dump(mode="json")
        )

    async def recipe(self, entity_id: str) -> dict | None:
        def get():
            result = self.database.recipe(entity_id)
            return result.model_dump(mode="json") if result else None

        return await self._run(get)

    async def meals(self, start, end) -> dict:
        return await self._run(
            lambda: self.database.meal_plan(start, end).model_dump(mode="json")
        )

    async def mealplans(self, start, end) -> dict:
        def read() -> dict:
            snapshot = read_sync_snapshot(self.database, self.editor.enabled)
            return {
                "mealplan": [
                    meal.model_dump(mode="json")
                    for meal in snapshot.meals
                    if start <= meal.date <= end
                ],
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "stale": snapshot.stale,
                "last_successful_sync_at": snapshot.last_successful_sync_at.isoformat(),
            }

        return await self._run(read)

    async def sync_snapshot(self) -> dict:
        return await self._run(
            lambda: read_sync_snapshot(self.database, self.editor.enabled).model_dump(
                mode="json"
            )
        )

    async def preview(self, start, end, staples=(), selected_keys=None) -> dict:
        return await self._run(
            lambda: select_ingredients(
                self.database.shopping_preview(start, end), staples, selected_keys
            ).model_dump(mode="json")
        )

    async def view(self, section: str, entity_id: str) -> dict:
        return await self._run(self.editor.view, section, entity_id)

    async def edit(
        self,
        section: str,
        action: str,
        operation_id: str,
        changes: dict,
        *,
        entity_id: str | None = None,
        expected_revision: str | None = None,
    ) -> dict:
        return await self._run(
            self.editor.edit,
            section,
            action,
            operation_id,
            changes,
            entity_id=entity_id,
            expected_revision=expected_revision,
        )

    async def operation(self, operation_id: str) -> dict | None:
        return await self._run(self.editor.operation, operation_id)

    async def plan_meal(self, operation_id: str, intent: dict) -> dict:
        return await self._run(self.automations.plan, operation_id, intent)

    async def import_recipe(
        self,
        operation_id: str,
        url: str,
        *,
        include_tags: bool = False,
        recipe_index: int | None = None,
    ) -> dict:
        intent = {
            "action": "import_recipe",
            "url": validate_url(url),
            "include_tags": include_tags,
            "recipe_index": recipe_index,
        }
        previous = await self._run(self.automations.prepared, operation_id, intent)
        if previous:
            return await self._run(self.automations.execute, operation_id, previous)
        html, final_url = await fetch_recipe_page(intent["url"])
        changes = await self.executor(
            partial(
                parse_recipe_page,
                html,
                final_url,
                include_tags=include_tags,
                recipe_index=recipe_index,
            )
        )
        return await self._run(
            self.automations.import_recipe, operation_id, intent, changes
        )

    async def reconcile(self, operation_id: str) -> dict:
        return await self._run(self.editor.reconcile, operation_id)

    async def close(self) -> None:
        self._closed = True
        self.transport.close()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)
