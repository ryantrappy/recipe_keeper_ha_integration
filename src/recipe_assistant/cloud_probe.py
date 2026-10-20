"""Experimental download-only cloud probe; independent of the database API."""

import argparse
import getpass
import json
import os
import plistlib
import sqlite3
import ssl
import sys
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .database import DEFAULT_DATABASE

SYNC_URL = "https://recipekeeper.azurewebsites.net/api/sync"
LOGIN_URL = "https://recipekeeper.azurewebsites.net/token"
INITIAL_CURSOR = "1601-01-01T00:00:00Z"
SECTIONS = ("Recipe", "Meal")
MAC_PREFERENCES = (
    Path.home()
    / "Library/Containers/com.tudorspan.recipekeeper-macos"
    / "Data/Library/Preferences/com.tudorspan.recipekeeper-macos.plist"
)


class ProbeError(Exception):
    """A cloud probe failed without exposing credentials or response contents."""


class AuthenticationError(ProbeError):
    """Credentials were rejected by the cloud service."""


class CloudHTTPError(ProbeError):
    def __init__(self, status_code: int, retry_after: int = 0) -> None:
        super().__init__(f"Cloud request failed (HTTP {status_code}); body omitted")
        self.status_code = status_code
        self.retry_after = retry_after


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Page:
    entities: list[dict]
    cursor: str
    has_more: bool


@dataclass
class Authentication:
    token: str = field(repr=False)
    expires_in_seconds: int | None
    issued_at_utc: str | None
    expires_at_utc: str | None
    refresh_token_returned: bool

    def summary(self) -> dict:
        return {
            "fresh_login": "succeeded",
            "access_token_received": True,
            "expires_in_seconds": self.expires_in_seconds,
            "issued_at_utc": self.issued_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "refresh_token_returned": self.refresh_token_returned,
            "credentials_saved": False,
        }


def expiration_time(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def fresh_login(email: str, password: str) -> Authentication:
    """Exchange explicitly entered credentials without touching the Mac session."""
    if not email.strip() or not password:
        raise ProbeError("Email and password are required")
    request = Request(
        LOGIN_URL,
        data=urlencode(
            {
                "username": email.strip(),
                "password": password,
                "grant_type": "password",
            }
        ).encode(),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-RK-APIVersion": "10",
            "User-Agent": "recipe-assistant-api/0.1 fresh authentication probe",
        },
    )
    opener = build_opener(
        NoRedirects(), HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(1_000_001)
    except HTTPError as exc:
        if exc.code in (400, 401):
            raise AuthenticationError(
                f"Login rejected (HTTP {exc.code}); "
                "check your Recipe Keeper credentials"
            ) from None
        raise ProbeError(
            f"Login failed (HTTP {exc.code}); response body omitted; no retry attempted"
        ) from None
    except (URLError, TimeoutError) as exc:
        raise ProbeError("Login network or TLS connection failed") from exc
    if len(raw) > 1_000_000:
        raise ProbeError("Login response exceeds 1 MB; stopping")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProbeError("Login response is not JSON") from exc
    if not isinstance(data, dict):
        raise ProbeError("Unexpected login response format")
    token = data.get("access_token")
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise ProbeError("Login response did not contain a usable access token")
    seconds = data.get("expires_in")
    expires_in = None
    if type(seconds) is int or (
        isinstance(seconds, str) and seconds.isascii() and seconds.isdecimal()
    ):
        expires_in = int(seconds)
    return Authentication(
        token=token,
        expires_in_seconds=expires_in,
        issued_at_utc=expiration_time(data.get(".issued")),
        expires_at_utc=expiration_time(data.get(".expires")),
        refresh_token_returned=bool(data.get("refresh_token")),
    )


def request_body(cursors: dict[str, str]) -> dict:
    if not cursors or set(cursors) - set(SECTIONS):
        raise ProbeError("Only recipe and meal downloads are supported")
    return {
        name + "SyncEntityRequest": {
            "MaxLastModified": cursor,
            "SyncEntities": [],
            "LocalChangesIds": [],
        }
        for name, cursor in cursors.items()
    }


def read_mac_session(path: Path = MAC_PREFERENCES) -> str:
    try:
        with path.open("rb") as file:
            settings = plistlib.load(file)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ProbeError("Cannot read the Mac session; check Full Disk Access") from exc
    token = settings.get("AccountBearerToken")
    if not isinstance(token, str) or not token:
        raise ProbeError("No cached bearer token; sign in through Recipe Keeper first")
    return token


class CloudProbe:
    def __init__(self, token: str) -> None:
        self._token = token
        self.calls = 0
        self.response_bytes = 0

    def _send(self, body: dict) -> dict:
        request = Request(
            SYNC_URL,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json; charset=utf-8",
                "X-RK-APIVersion": "10",
                "User-Agent": "recipe-assistant-api/0.1 read-only feasibility probe",
            },
        )
        opener = build_opener(
            NoRedirects(), HTTPSHandler(context=ssl.create_default_context())
        )
        try:
            with opener.open(request, timeout=45) as response:
                raw = response.read(50_000_001)
        except HTTPError as exc:
            retry = exc.headers.get("Retry-After", "") if exc.headers else ""
            retry_after = int(retry) if retry.isascii() and retry.isdecimal() else 0
            raise CloudHTTPError(exc.code, retry_after) from None
        except (URLError, TimeoutError) as exc:
            raise ProbeError("Cloud network or TLS connection failed") from exc
        self.calls += 1
        self.response_bytes += len(raw)
        if len(raw) > 50_000_000:
            raise ProbeError("Cloud page exceeds the probe's 50 MB response limit")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProbeError("Cloud response is not JSON") from exc
        if not isinstance(data, dict):
            raise ProbeError("Unexpected cloud response format")
        return data

    def page(self, cursors: dict[str, str]) -> dict[str, Page]:
        data = self._send(request_body(cursors))
        result = {}
        for name, previous in cursors.items():
            part = data.get(name + "SyncEntityResponse")
            if not isinstance(part, dict):
                raise ProbeError(f"Missing {name} response")
            if part.get("UpdatedLocalItems") != []:
                raise ProbeError("Unexpected upload acknowledgements; stopping")
            rows = part.get("UpdatedServerItems")
            cursor = part.get("MaxLastModified")
            more = part.get("HasMoreUpdatedServerItems")
            if (
                not isinstance(rows, list)
                or any(
                    not isinstance(row, dict)
                    or not isinstance(row.get("Id"), str)
                    or type(row.get("IsDeleted", False)) is not bool
                    for row in rows
                )
                or not isinstance(cursor, str)
                or not cursor
                or not isinstance(more, bool)
            ):
                raise ProbeError(f"Unexpected {name} response fields")
            if more and cursor == previous:
                raise ProbeError("Pagination cursor did not advance")
            # Photo blobs are not needed for feasibility checks or ingredient lists.
            stripped = [
                {
                    key: value
                    for key, value in row.items()
                    if not key.startswith("Photo")
                }
                for row in rows
            ]
            result[name] = Page(stripped, cursor, more)
        return result

    def download(self, max_pages: int = 20) -> tuple[dict, dict[str, str]]:
        pending = dict.fromkeys(SECTIONS, INITIAL_CURSOR)
        cursors = {}
        entities: dict[str, dict[str, dict]] = {name: {} for name in SECTIONS}
        for _ in range(max_pages):
            for name, page in self.page(pending).items():
                entities[name].update((row["Id"], row) for row in page.entities)
                cursors[name] = page.cursor
                if page.has_more:
                    pending[name] = page.cursor
                else:
                    del pending[name]
            if not pending:
                return entities, cursors
        raise ProbeError("Download page limit reached; incomplete results discarded")


def compare_local(entities: dict, path: Path) -> dict:
    """Compare core text fields and IDs; return only aggregate mismatch counts."""
    comparisons = {
        "Recipe": ("Recipes", ("Title", "Ingredients", "Directions", "ServingSize")),
        "Meal": ("Meals", ("Title", "MealTypeName", "Scale", "Notes")),
    }
    result = {}
    conn = sqlite3.connect(path.expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for name, (table, fields) in comparisons.items():
            rows = {
                row["Id"]: dict(row)
                for row in conn.execute(
                    f"SELECT Id, {', '.join(fields)} FROM {table} WHERE IsDeleted=0"
                )
            }
            active = {
                key: row
                for key, row in entities[name].items()
                if not row.get("IsDeleted")
            }
            shared = rows.keys() & active.keys()
            result[name] = {
                "local_active": len(rows),
                "cloud_active": len(active),
                "only_local": len(rows.keys() - active.keys()),
                "only_cloud": len(active.keys() - rows.keys()),
                "text_mismatches": sum(
                    any(
                        (rows[key][field] or "") != (active[key].get(field) or "")
                        for field in fields
                    )
                    for key in shared
                ),
            }
    finally:
        conn.close()
    return result


def summarize(probe: CloudProbe, database_path: Path | None = None) -> dict:
    entities, cursors = probe.download()
    downloaded_bytes = probe.response_bytes
    result = {
        "download": {
            name: {
                "total_entities": len(rows),
                "active_entities": sum(
                    not row.get("IsDeleted") for row in rows.values()
                ),
                "deleted_entities": sum(
                    bool(row.get("IsDeleted")) for row in rows.values()
                ),
            }
            for name, rows in entities.items()
        },
        "initial_response_bytes": downloaded_bytes,
    }
    if database_path is not None:
        result["local_comparison"] = compare_local(entities, database_path)
    delta = probe.page(cursors)
    result["incremental_check"] = {
        name: {"changed_entities": len(page.entities), "has_more": page.has_more}
        for name, page in delta.items()
    }
    result["incremental_response_bytes"] = probe.response_bytes - downloaded_bytes
    result["http_requests"] = probe.calls
    result["uploaded_entities"] = 0
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument(
        "--mac-session",
        action="store_true",
        help="Read the signed-in Mac app's cached bearer token without printing it",
    )
    authentication.add_argument(
        "--login",
        action="store_true",
        help="Prompt privately for credentials and test fresh login plus downloads",
    )
    parser.add_argument(
        "--compare-database",
        type=Path,
        nargs="?",
        const=DEFAULT_DATABASE,
        help="Compare IDs and core text fields with a local read-only database",
    )
    args = parser.parse_args()
    try:
        report = {}
        if args.login:
            if not sys.stdin.isatty():
                raise ProbeError("--login requires an interactive terminal")
            print(
                "Enter your Recipe Keeper account details; both inputs are hidden.",
                file=sys.stderr,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                email = getpass.getpass("Recipe Keeper email: ")
                password = getpass.getpass("Recipe Keeper password: ")
            auth = fresh_login(email, password)
            del email, password
            token = auth.token
            report["authentication"] = auth.summary()
            print(
                "Fresh login succeeded; testing download-only synchronization...",
                file=sys.stderr,
            )
        else:
            token = (
                read_mac_session()
                if args.mac_session
                else os.environ.get("RECIPE_KEEPER_CLOUD_TOKEN", "")
            )
        if not token:
            raise ProbeError(
                "Use --login, --mac-session, or set RECIPE_KEEPER_CLOUD_TOKEN"
            )
        report.update(summarize(CloudProbe(token), args.compare_database))
        print(json.dumps(report, indent=2))
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, "Probe cancelled\n")
    except getpass.GetPassWarning:
        parser.exit(1, "Cannot hide credential input; use an interactive terminal\n")
    except (ProbeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Probe failed: {exc}\n")


if __name__ == "__main__":
    main()
