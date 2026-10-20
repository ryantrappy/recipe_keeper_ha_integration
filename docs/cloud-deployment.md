# Run the cloud backend

Cloud mode runs on Linux or macOS and does not read the Recipe Keeper app's
database, preferences, or Mac session. It signs in independently, downloads
recipe/meal updates, and serves the existing API from its own persistent cache.

## Configure on this Mac

```sh
cd /Users/ryantrapp/Code/recipe_assistant_api
uv sync --locked
uv run recipe-assistant configure-cloud
uv run recipe-assistant sync --backend cloud
uv run recipe-assistant serve --backend cloud
```

`configure-cloud` prompts for your Recipe Keeper email and password with hidden
input. It saves them to `local-data/cloud-credentials.json` with mode `0600`
inside a Git-ignored directory. This is a plaintext credentials file protected
by filesystem permissions, not an encrypted keychain. Nothing is printed except
the file path. Existing credentials require `--replace-credentials` to replace.
Restart the service after changing credentials.

If `.env` does not exist yet, run `uv run recipe-assistant init` first to create
the separate local API bearer token. This is not the Recipe Keeper password or
cloud token. Keep existing `.env` files; `init` refuses to overwrite them.

To make cloud mode the default, add `RECIPE_ASSISTANT_BACKEND=cloud` to `.env`.
Otherwise, pass `--backend cloud` explicitly. Database mode remains available as
`--backend database`.

## Configuration

| Variable | Default / behavior |
| --- | --- |
| `RECIPE_ASSISTANT_BACKEND` | `database`; set to `cloud` |
| `RECIPE_KEEPER_CREDENTIALS_FILE` | `local-data/cloud-credentials.json` |
| `RECIPE_KEEPER_EMAIL` and `RECIPE_KEEPER_PASSWORD` | Optional deployment-supplied credentials; both override the file |
| `RECIPE_KEEPER_CACHE` | `local-data/cloud-cache.db` |
| `RECIPE_KEEPER_POLL_SECONDS` | `300`; minimum 60 |
| `RECIPE_ASSISTANT_HOST` | `127.0.0.1`; choose an appropriate private address for remote clients |
| `RECIPE_ASSISTANT_PORT` | `8765` |

Paths are relative to the working directory unless absolute. The process reads
credentials at startup. Cloud tokens are held in memory and obtained again
after a process restart. The cache is private (`0600`) and bound to a hash of
the account email. Changing to a different account requires a different cache
path; the service refuses to mix accounts. Keep caches out of Git and public
backups: they contain your recipes and meal plans.

An existing arbitrary SQLite file is refused as a cache. The service creates
and writes only its own schema, with a bridge-specific application identifier.
Do not set the cache path to Recipe Keeper's database.

## Startup, incremental sync, and failures

The first sync downloads all recipes and meals. Subsequent syncs use saved
per-section cursors, preserving their original 100 ns precision. Pages are
downloaded before acquiring a database write transaction. The entire set of
updates, deletions, and cursors commits together. Failed or incomplete page
downloads leave the last complete snapshot and cursors intact.

No photo blobs are stored in this cache. The server can include photos in the
initial response, so it can still be large. Ingredients, directions, serving
text, categories, linked recipe IDs, and meal fields used by the existing API
are retained. Cloud planner dates keep their calendar date without a timezone
conversion. The cache does not implement every Recipe Keeper feature.

`serve` syncs in a background thread, first immediately and then every five
minutes by default. A sync 401 causes one fresh login and one retry of that
same request with the same cursors. A second 401 becomes an authentication
failure. Network/service failures use exponential backoff, normally capped at
30 minutes; a larger configured polling interval remains the lower bound.
Authentication failures wait at least 30 minutes before another attempt.
Numeric `Retry-After` values from sync responses are respected. There is no
immediate login loop, nor a full reset of the cache on token renewal.

The local API remains available during cloud outages once a complete snapshot
exists. Before the first successful sync, data endpoints return 503 rather
than an empty collection. `/v1/status` stays available and reports:

- `backend`: `cloud`.
- `cache_ready`: whether a complete snapshot exists.
- `stale`: true after a failed sync or when the last success is older than two
  polling intervals; also true before the first successful sync.
- `last_sync_attempt_at`, `last_successful_sync_at`, and `next_sync_at`.
- `last_sync_error`: a generic error category; no credentials or server body.

Cloud responses also include `X-Recipe-Assistant-Stale` and, when available,
`X-Recipe-Assistant-Last-Sync`. Consumers should inspect freshness before adding
ingredients automatically. `cloud_sync_verified` is true only for a complete,
currently fresh cloud cache; it cannot prove another device has uploaded its
latest changes. The status clock is in UTC.

`uv run recipe-assistant inspect --backend cloud` inspects cache status without
making a network request. `sync --backend cloud` performs one refresh and prints
status, returning a nonzero exit code on failure. It does not retry in a loop.
A file lock prevents concurrent sync processes from racing against this cache.
Use one serving process per configured cache; an additional inspection is safe.

## Run on trappserv.er

Put the project at `~/recipe_assistant_api` on trappserv.er (or adjust the unit's
paths), install uv, and run:

```sh
cd ~/recipe_assistant_api
uv sync --locked
uv run recipe-assistant init
uv run recipe-assistant configure-cloud
uv run recipe-assistant sync --backend cloud
```

Create fresh local credentials there instead of copying the Mac's app files,
virtual environment, or live cache. Keep the local API token in `.env`. Verify
the chosen listener address is reachable from Home Assistant's actual runtime;
a container or VM has its own loopback address. For transport choices, see
the network section in [deployment.md](deployment.md).

The supplied user systemd unit assumes `~/recipe_assistant_api`:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/recipe-assistant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now recipe-assistant.service
journalctl --user -u recipe-assistant.service -f
```

Enable lingering for this Linux user if the service should remain running after
logout: `loginctl enable-linger "$USER"`. The unit has not been installed by
Codex. Restart after code or credential changes with
`systemctl --user restart recipe-assistant.service`.

The Mac launchd renderer also works for cloud mode when `.env` contains
`RECIPE_ASSISTANT_BACKEND=cloud`. Follow the Mac startup instructions in
[deployment.md](deployment.md).

This is an implementation of the investigated private sync protocol. Live
downloads and cache reads were verified on macOS; deployment on trappserv.er
still needs its credentials, network check, and service installation.
