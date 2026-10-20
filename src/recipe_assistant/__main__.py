"""CLI for configuration, diagnostics, and serving."""

import argparse
import getpass
import json
import os
import secrets
import sqlite3
import sys
import warnings
from pathlib import Path

from dotenv import load_dotenv

from .cloud_backend import (
    CloudCache,
    CloudDatabase,
    CloudSession,
    SyncWorker,
    load_credentials,
)
from .cloud_edits import CloudEditor
from .database import DEFAULT_DATABASE, Database, SchemaError
from .shopping import parse_staples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("init", "inspect", "serve", "configure-cloud", "sync")
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--backend", choices=("database", "cloud"))
    parser.add_argument("--replace-credentials", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        try:
            fd = os.open(args.env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            parser.error(f"{args.env_file} already exists; keeping its token")
        with os.fdopen(fd, "w") as config:
            config.write(
                f"RECIPE_ASSISTANT_TOKEN={secrets.token_urlsafe(32)}\n"
                "RECIPE_ASSISTANT_HOST=127.0.0.1\n"
                "RECIPE_ASSISTANT_PORT=8765\n"
                "RECIPE_ASSISTANT_ALLOW_EDITS=false\n"
            )
        print(f"Created {args.env_file} (mode 0600); token was not printed")
        return
    load_dotenv(args.env_file)
    credentials_path = Path(
        os.environ.get(
            "RECIPE_KEEPER_CREDENTIALS_FILE", "local-data/cloud-credentials.json"
        )
    ).expanduser()
    if args.command == "configure-cloud":
        if not sys.stdin.isatty():
            parser.error("configure-cloud requires an interactive terminal")
        if credentials_path.exists() and not args.replace_credentials:
            parser.error(
                "Credentials already exist; use --replace-credentials to rotate them"
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                email = getpass.getpass("Recipe Keeper email (hidden): ")
                password = getpass.getpass("Recipe Keeper password (hidden): ")
        except (getpass.GetPassWarning, KeyboardInterrupt, EOFError):
            parser.exit(1, "Credential setup cancelled; hidden input is required\n")
        if not email.strip() or not password:
            parser.error("Email and password are required")
        credentials_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = credentials_path.with_name(credentials_path.name + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump({"email": email.strip(), "password": password}, file)
            os.replace(temporary, credentials_path)
        finally:
            temporary.unlink(missing_ok=True)
        print(
            f"Saved private credentials to {credentials_path}; "
            "start with --backend cloud"
        )
        return
    backend = args.backend or os.environ.get("RECIPE_ASSISTANT_BACKEND", "database")
    worker = None
    editor = None
    try:
        editing_value = os.environ.get("RECIPE_ASSISTANT_ALLOW_EDITS", "false").lower()
        if editing_value not in ("true", "false"):
            parser.error("RECIPE_ASSISTANT_ALLOW_EDITS must be true or false")
        allow_edits = editing_value == "true"
        if backend == "cloud":
            if args.database is not None:
                parser.error("Use RECIPE_KEEPER_CACHE for cloud mode, not --database")
            poll = int(os.environ.get("RECIPE_KEEPER_POLL_SECONDS", "300"))
            if poll < 60:
                parser.error("RECIPE_KEEPER_POLL_SECONDS must be at least 60")
            credentials = load_credentials(credentials_path)
            cache = CloudCache(
                Path(
                    os.environ.get("RECIPE_KEEPER_CACHE", "local-data/cloud-cache.db")
                ),
                credentials.account_id,
            )
            database = CloudDatabase(cache, poll)
            worker = SyncWorker(
                cache, CloudSession(credentials, allow_edits=allow_edits), poll
            )
            editor = CloudEditor(worker, allow_edits=allow_edits)
        elif backend == "database":
            if allow_edits:
                parser.error(
                    "Editing requires the cloud backend; database mode is read-only"
                )
            database = Database(
                args.database
                or Path(os.environ.get("RECIPE_KEEPER_DB", str(DEFAULT_DATABASE)))
            )
        else:
            parser.error("RECIPE_ASSISTANT_BACKEND must be database or cloud")
        if args.command == "sync":
            if worker is None:
                parser.error("sync requires --backend cloud")
            worker.run_once()
        status = database.status()
        if editor is not None:
            status.allow_edits = editor.enabled
            status.edit_operations = editor.counts()
    except (OSError, sqlite3.Error, SchemaError, ValueError) as exc:
        parser.exit(
            1,
            f"Cannot initialize backend ({type(exc).__name__}); check configuration, "
            "file permissions, and account/cache selection.\n",
        )
    if args.command in ("inspect", "sync"):
        print(
            json.dumps(
                {"database": str(database.path), **status.model_dump(mode="json")},
                indent=2,
            )
        )
        if args.command == "sync" and status.last_sync_error:
            parser.exit(1)
        return
    from .api import create_app

    try:
        staples = parse_staples(os.environ.get("RECIPE_ASSISTANT_PANTRY_STAPLES", "[]"))
        app = create_app(
            database,
            os.environ.get("RECIPE_ASSISTANT_TOKEN", ""),
            worker,
            staples,
            editor,
        )
        port = int(os.environ.get("RECIPE_ASSISTANT_PORT", "8765"))
    except ValueError as exc:
        parser.error(str(exc))
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("RECIPE_ASSISTANT_HOST", "127.0.0.1"),
        port=port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
