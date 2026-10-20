"""Persist download-only cloud syncs and serve them through the existing reader."""

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .cloud_probe import (
    INITIAL_CURSOR,
    SECTIONS,
    AuthenticationError,
    CloudHTTPError,
    CloudProbe,
    Page,
    ProbeError,
    fresh_login,
)
from .database import DEFAULT_DATABASE, Database, SchemaError, ticks
from .models import Status

LOGGER = logging.getLogger(__name__)
APPLICATION_ID = 0x524B4841
SCHEMA_VERSION = 1


class CacheUnavailable(Exception):
    """No complete cloud snapshot is available yet."""


class SyncBusy(Exception):
    """Another process is already syncing this cache."""


@dataclass(frozen=True)
class Credentials:
    email: str = field(repr=False)
    password: str = field(repr=False)

    @property
    def account_id(self) -> str:
        return hashlib.sha256(self.email.strip().casefold().encode()).hexdigest()


def load_credentials(path: Path) -> Credentials:
    email = os.environ.get("RECIPE_KEEPER_EMAIL")
    password = os.environ.get("RECIPE_KEEPER_PASSWORD")
    if email is None and password is None:
        if path.stat().st_mode & 0o077:
            raise ValueError("Cloud credentials file must have mode 0600")
        try:
            data = json.loads(path.read_text())
            email, password = data["email"], data["password"]
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("Invalid cloud credentials file") from exc
    if (
        not isinstance(email, str)
        or not email.strip()
        or not isinstance(password, str)
        or not password
    ):
        raise ValueError("Supply both Recipe Keeper email and password")
    return Credentials(email.strip(), password)


class CloudSession(CloudProbe):
    def __init__(self, credentials: Credentials) -> None:
        super().__init__("")
        self.credentials = credentials

    def page(self, cursors: dict[str, str]) -> dict[str, Page]:
        if not self._token:
            self._token = fresh_login(
                self.credentials.email, self.credentials.password
            ).token
        try:
            return super().page(cursors)
        except CloudHTTPError as exc:
            if exc.status_code != 401:
                raise
        self._token = fresh_login(
            self.credentials.email, self.credentials.password
        ).token
        try:
            return super().page(cursors)
        except CloudHTTPError as exc:
            if exc.status_code == 401:
                self._token = ""
                raise AuthenticationError("Renewed session rejected") from None
            raise


class CloudCache:
    def __init__(self, path: Path, account_id: str) -> None:
        self.path = path.expanduser().resolve()
        self.account_id = account_id
        if self.path == DEFAULT_DATABASE.resolve():
            raise ValueError("The cloud cache must not be Recipe Keeper's database")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            with self.connect() as conn:
                if (
                    conn.execute("PRAGMA application_id").fetchone()[0]
                    != APPLICATION_ID
                ):
                    raise SchemaError(
                        "Cache file is not owned by Recipe Assistant"
                    ) from None
                if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise SchemaError("Unsupported cloud cache version") from None
                stored = conn.execute(
                    "SELECT value FROM Metadata WHERE key='account_id'"
                ).fetchone()
                if stored is None or json.loads(stored[0]) != account_id:
                    raise ValueError(
                        "Cloud cache belongs to a different account; "
                        "choose another cache path"
                    ) from None
            if self.path.stat().st_mode & 0o077:
                raise ValueError("Cloud cache must have mode 0600") from None
        else:
            os.close(fd)
            with self.connect() as conn:
                conn.executescript(f"""
                    PRAGMA application_id={APPLICATION_ID};
                    PRAGMA user_version={SCHEMA_VERSION};
                    CREATE TABLE Metadata (key TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE Recipes (
                        Id TEXT PRIMARY KEY, IsDeleted INTEGER DEFAULT 0,
                        Title TEXT, ServingSize TEXT, IsFavourite INTEGER, Source TEXT,
                        PreparationTime INTEGER, CookingTime INTEGER,
                        Ingredients TEXT, Directions TEXT, Notes TEXT
                    );
                    CREATE TABLE Meals (
                        Id TEXT PRIMARY KEY, IsDeleted INTEGER DEFAULT 0,
                        MealDate INTEGER, Title TEXT, MealTypeName TEXT, Scale TEXT,
                        Notes TEXT, Position INTEGER, MealTime TEXT
                    );
                    CREATE TABLE RecipeCategories (RecipeId TEXT, Name TEXT);
                    CREATE TABLE LinkedRecipes (
                        Id INTEGER PRIMARY KEY, RecipeId TEXT, LinkedRecipeId TEXT
                    );
                """)
                self._metadata(conn, {"account_id": account_id})
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def sync_lock(self) -> Iterator[None]:
        lock_path = Path(str(self.path) + ".sync.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SyncBusy from None
            yield

    @staticmethod
    def _metadata(conn: sqlite3.Connection, values: dict) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO Metadata (key, value) VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in values.items()],
        )

    def metadata(self) -> dict:
        with self.connect() as conn:
            return {
                key: json.loads(value)
                for key, value in conn.execute("SELECT key, value FROM Metadata")
            }

    def update_metadata(self, values: dict) -> None:
        with self.connect() as conn:
            self._metadata(conn, values)

    def apply(self, entities: dict, cursors: dict[str, str], now: datetime) -> None:
        with self.connect() as conn:
            for row in entities["Recipe"].values():
                recipe_id = row["Id"]
                conn.execute(
                    "DELETE FROM RecipeCategories WHERE RecipeId=?", (recipe_id,)
                )
                conn.execute("DELETE FROM LinkedRecipes WHERE RecipeId=?", (recipe_id,))
                if row.get("IsDeleted"):
                    conn.execute("DELETE FROM Recipes WHERE Id=?", (recipe_id,))
                    continue
                columns = (
                    "Id",
                    "Title",
                    "ServingSize",
                    "IsFavourite",
                    "Source",
                    "PreparationTime",
                    "CookingTime",
                    "Ingredients",
                    "Directions",
                    "Notes",
                )
                conn.execute(
                    f"INSERT OR REPLACE INTO Recipes ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    tuple(row.get(column) for column in columns),
                )
                conn.executemany(
                    "INSERT INTO RecipeCategories VALUES (?, ?)",
                    [(recipe_id, name) for name in row.get("Categories") or []],
                )
                conn.executemany(
                    "INSERT INTO LinkedRecipes (RecipeId, LinkedRecipeId) "
                    "VALUES (?, ?)",
                    [
                        (recipe_id, linked)
                        for linked in row.get("LinkedRecipeIds") or []
                    ],
                )
            for row in entities["Meal"].values():
                if row.get("IsDeleted"):
                    conn.execute("DELETE FROM Meals WHERE Id=?", (row["Id"],))
                    continue
                value = row.get("MealDate")
                if not isinstance(value, str):
                    raise ProbeError("Unsupported cloud meal date")
                try:
                    meal_date = datetime.fromisoformat(value).date()
                except ValueError as exc:
                    raise ProbeError("Unsupported cloud meal date") from exc
                conn.execute(
                    "INSERT OR REPLACE INTO Meals (Id, MealDate, Title, MealTypeName, "
                    "Scale, Notes, Position, MealTime) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["Id"],
                        ticks(meal_date),
                        row.get("Title"),
                        row.get("MealTypeName"),
                        row.get("Scale"),
                        row.get("Notes"),
                        row.get("Position"),
                        row.get("MealTime"),
                    ),
                )
            self._metadata(
                conn,
                {
                    "cursors": cursors,
                    "last_successful_sync_at": now.isoformat(),
                    "last_sync_error": None,
                },
            )


class CloudDatabase(Database):
    def __init__(self, cache: CloudCache, poll_seconds: int) -> None:
        super().__init__(cache.path)
        self.cache = cache
        self.poll_seconds = poll_seconds

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with super().connection() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM Metadata WHERE key='last_successful_sync_at'"
                ).fetchone()
                is None
            ):
                raise CacheUnavailable
            yield conn

    def status(self) -> Status:
        base = Database(self.path).status()
        metadata = self.cache.metadata()
        last = metadata.get("last_successful_sync_at")
        last_sync = datetime.fromisoformat(last) if last else None
        stale = (
            last_sync is None
            or bool(metadata.get("last_sync_error"))
            or (datetime.now(UTC) - last_sync).total_seconds() > self.poll_seconds * 2
        )
        return Status.model_validate(
            {
                **base.model_dump(),
                "backend": "cloud",
                "status": "degraded" if stale else "ok",
                "cache_ready": last_sync is not None,
                "stale": stale,
                "cloud_sync_verified": last_sync is not None and not stale,
                "last_sync_attempt_at": metadata.get("last_sync_attempt_at"),
                "last_successful_sync_at": last_sync,
                "last_sync_error": metadata.get("last_sync_error"),
                "next_sync_at": metadata.get("next_sync_at"),
            }
        )


class SyncWorker:
    def __init__(
        self, cache: CloudCache, session: CloudSession, poll_seconds: int = 300
    ) -> None:
        self.cache = cache
        self.session = session
        self.poll_seconds = poll_seconds
        self.failures = 0
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def sync(self) -> None:
        with self.cache.sync_lock():
            now = datetime.now(UTC)
            self.cache.update_metadata({"last_sync_attempt_at": now.isoformat()})
            previous = self.cache.metadata().get("cursors", {})
            pending = {name: previous.get(name, INITIAL_CURSOR) for name in SECTIONS}
            cursors = dict(previous)
            entities = {name: {} for name in SECTIONS}
            for _ in range(100):
                pages = self.session.page(dict(pending))
                if self.stop_event.is_set():
                    return
                for name, page in pages.items():
                    entities[name].update((row["Id"], row) for row in page.entities)
                    cursors[name] = page.cursor
                    if page.has_more:
                        pending[name] = page.cursor
                    else:
                        del pending[name]
                if not pending:
                    self.cache.apply(entities, cursors, datetime.now(UTC))
                    return
            raise ProbeError("Cloud pagination limit reached")

    def run_once(self) -> int:
        try:
            self.sync()
        except SyncBusy:
            return self.poll_seconds
        except (ProbeError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
            self.failures += 1
            reason = (
                "authentication"
                if isinstance(exc, AuthenticationError)
                else "cloud_or_cache"
            )
            delay = min(
                self.poll_seconds * 2 ** min(self.failures, 10),
                max(self.poll_seconds, 1800),
            )
            if isinstance(exc, AuthenticationError):
                delay = max(delay, 1800)
            if isinstance(exc, CloudHTTPError):
                delay = max(delay, exc.retry_after)
            try:
                self.cache.update_metadata(
                    {
                        "last_sync_error": reason,
                        "next_sync_at": (
                            datetime.now(UTC) + timedelta(seconds=delay)
                        ).isoformat(),
                    }
                )
            except (OSError, sqlite3.Error):
                LOGGER.warning("Unable to persist cloud failure status")
            LOGGER.warning("Cloud sync failed (%s); retry in %s seconds", reason, delay)
            return delay
        self.failures = 0
        try:
            self.cache.update_metadata(
                {
                    "next_sync_at": (
                        datetime.now(UTC) + timedelta(seconds=self.poll_seconds)
                    ).isoformat()
                }
            )
        except (OSError, sqlite3.Error):
            LOGGER.warning("Unable to persist cloud schedule")
        return self.poll_seconds

    def start(self) -> None:
        def run() -> None:
            while not self.stop_event.is_set():
                delay = self.run_once()
                self.stop_event.wait(delay)

        self.thread = threading.Thread(
            target=run, name="recipe-cloud-sync", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
