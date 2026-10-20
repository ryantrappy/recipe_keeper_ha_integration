"""CLI for configuration, diagnostics, and serving."""

import argparse
import json
import os
import secrets
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from .database import DEFAULT_DATABASE, Database, SchemaError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "inspect", "serve"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--database", type=Path)
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
            )
        print(f"Created {args.env_file} (mode 0600); token was not printed")
        return
    load_dotenv(args.env_file)
    database = Database(
        args.database or Path(os.environ.get("RECIPE_KEEPER_DB", str(DEFAULT_DATABASE)))
    )
    try:
        status = database.status()
    except (OSError, sqlite3.Error, SchemaError) as exc:
        parser.exit(
            1,
            f"Cannot read Recipe Keeper database: {exc}\n"
            "Check the path and macOS Full Disk Access for this process.\n",
        )
    if args.command == "inspect":
        print(
            json.dumps(
                {"database": str(database.path), **status.model_dump(mode="json")},
                indent=2,
            )
        )
        return
    from .api import create_app

    try:
        app = create_app(database, os.environ.get("RECIPE_ASSISTANT_TOKEN", ""))
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
