"""Shared cloud authentication and synchronization protocol."""

import json
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

SYNC_URL = "https://recipekeeper.azurewebsites.net/api/sync"
LOGIN_URL = "https://recipekeeper.azurewebsites.net/token"
INITIAL_CURSOR = "1601-01-01T00:00:00Z"
SECTIONS = ("Recipe", "Meal")


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
    except (URLError, TimeoutError, HTTPException):
        raise ProbeError("Login network, TLS or HTTP connection failed") from None
    if len(raw) > 1_000_000:
        raise ProbeError("Login response exceeds 1 MB; stopping")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProbeError("Login response is not JSON") from exc
    return parse_authentication(data)


def parse_authentication(data: object) -> Authentication:
    """Validate a decoded authentication reply without retaining its other fields."""
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


class CloudProbe:
    def __init__(self, token: str) -> None:
        self._token = token
        self.preserve_photos = False
        self.user_agent = "recipe-assistant-api/0.1 read-only feasibility probe"
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
                "User-Agent": self.user_agent,
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
        except (URLError, TimeoutError, HTTPException):
            raise ProbeError("Cloud network, TLS or HTTP connection failed") from None
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
            stripped = (
                rows
                if self.preserve_photos
                else [
                    {
                        key: value
                        for key, value in row.items()
                        if not key.startswith("Photo")
                    }
                    for row in rows
                ]
            )
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
