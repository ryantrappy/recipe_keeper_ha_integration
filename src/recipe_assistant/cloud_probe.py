"""Experimental download-only cloud probe; independent of the database API."""

import argparse
import getpass
import json
import os
import plistlib
import sqlite3
import sys
import warnings
from pathlib import Path

from .cloud_protocol import (
    INITIAL_CURSOR as INITIAL_CURSOR,
)
from .cloud_protocol import (
    LOGIN_URL as LOGIN_URL,
)
from .cloud_protocol import (
    SECTIONS as SECTIONS,
)
from .cloud_protocol import (
    SYNC_URL as SYNC_URL,
)
from .cloud_protocol import (
    Authentication as Authentication,
)
from .cloud_protocol import (
    AuthenticationError as AuthenticationError,
)
from .cloud_protocol import (
    CloudHTTPError as CloudHTTPError,
)
from .cloud_protocol import (
    CloudProbe as CloudProbe,
)
from .cloud_protocol import (
    NoRedirects as NoRedirects,
)
from .cloud_protocol import (
    Page as Page,
)
from .cloud_protocol import (
    ProbeError as ProbeError,
)
from .cloud_protocol import (
    expiration_time as expiration_time,
)
from .cloud_protocol import (
    fresh_login as fresh_login,
)
from .cloud_protocol import (
    parse_authentication as parse_authentication,
)
from .cloud_protocol import (
    request_body as request_body,
)
from .database import DEFAULT_DATABASE

MAC_PREFERENCES = (
    Path.home()
    / "Library/Containers/com.tudorspan.recipekeeper-macos"
    / "Data/Library/Preferences/com.tudorspan.recipekeeper-macos.plist"
)


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
