# Recipe Assistant API

A read-only Recipe Keeper API for Home Assistant or other local clients. Choose
the Mac's local database or a persistent cloud cache that can run independently
on trappserv.er. Both backends expose the same recipe, meal-plan, and ingredient
preview endpoints. Cloud mode signs in automatically, polls incrementally, and
keeps serving its last complete snapshot during outages.

See [cloud deployment](docs/cloud-deployment.md) for the recommended cloud setup
and [the investigation](docs/cloud-investigation.md) for protocol findings.
The default remains local database mode; it does not need cloud credentials.

[HACS feasibility and architecture](docs/hacs-feasibility.md) confirms a future
custom integration can connect directly from Home Assistant in the VM, using
HA networking and storage. It does not require a separate API container. The
current API remains useful independently; the HACS component is not built yet.

## Cloud quick start

```sh
cd /Users/ryantrapp/Code/recipe_assistant_api
uv sync --locked
uv run recipe-assistant configure-cloud
uv run recipe-assistant sync --backend cloud
uv run recipe-assistant serve --backend cloud
```

Run `uv run recipe-assistant init` first if this is a new checkout without `.env`.
Cloud setup saves credentials in a private, Git-ignored file with mode `0600`.
Cloud responses report freshness in `/v1/status` and the
`X-Recipe-Assistant-Stale` header. Read the deployment guide before enabling
automatic shopping-list changes against a stale cache.

To test fresh cloud authentication from an interactive terminal:

```sh
uv run python -m recipe_assistant.cloud_probe --login
```

It prompts privately for your Recipe Keeper account email/password, reports
expiration metadata, and tests download-only sync. Credentials are not saved.
See the investigation document for details and limitations.

## Local database quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
cd /Users/ryantrapp/Code/recipe_assistant_api
uv sync --locked
uv run recipe-assistant init
uv run recipe-assistant inspect
uv run recipe-assistant serve
```

`init` creates a Git-ignored `.env` with a random bearer token and mode `0600`.
It refuses to overwrite an existing configuration. The server listens at
`http://127.0.0.1:8765`. The API reference is at `/docs`; use **Authorize** and
the token from `.env` to try requests. The reference page itself is public;
every data endpoint requires `Authorization: Bearer <token>`.

For a shell request without placing the token in the command text or URL:

```sh
set -a
source .env
set +a
curl --fail --silent --show-error \
  -H "Authorization: Bearer $RECIPE_ASSISTANT_TOKEN" \
  'http://127.0.0.1:8765/v1/meal-plan?start=2026-10-19&end=2026-10-25'
```

### Database permissions

The default database is:

```text
~/Library/Containers/com.tudorspan.recipekeeper-macos/Data/Documents/RecipeKeeper.db
```

macOS may require **System Settings → Privacy & Security → Full Disk Access**
for the process launching the bridge, then quitting and reopening that process.
A service started by launchd can have different permissions from Terminal or
Codex. Check the service error log if it works interactively but not in launchd;
the Python executable used by the service may need access separately.

Override the path with `RECIPE_KEEPER_DB` in `.env`, or use
`recipe-assistant inspect --database /absolute/path/RecipeKeeper.db`.
For an offline copy, quit Recipe Keeper before copying the database. Do not
copy a live SQLite file on its own: committed data may be in its `-wal` file.
The bridge can read the live database directly, including WAL transactions.

## Endpoints

| GET endpoint | Result |
| --- | --- |
| `/v1/status` | Active counts, database modification time, availability |
| `/v1/recipes?q=lemon&limit=25&offset=0` | Search titles and ingredients; paged summaries |
| `/v1/recipes/{id}` | Ingredients, directions, serving text, notes, source, categories, linked recipe IDs |
| `/v1/meal-plan?start=YYYY-MM-DD&end=YYYY-MM-DD` | Meals with recipe match status and full matched recipes |
| `/v1/shopping-preview?start=YYYY-MM-DD&end=YYYY-MM-DD` | Ingredient lines with provenance, stable keys, and review issues |

Shopping previews also support pantry defaults and explicit ingredient choices.
Use `selected_items` for the intended shopping subset, and POST `selected_keys`
to `/v1/shopping-preview` to change those choices. See
[ingredient selection](docs/shopping-selection.md). Pantry staples stay visible
but unchecked by default; recipes themselves are unchanged.

Date ranges include both endpoints and are limited to 366 calendar days.
Dates are the planner's local calendar dates; they are not shifted into UTC.
Search matches every whitespace-separated word, case-insensitively, anywhere
in a recipe's title or ingredients. `%` and `_` are literal characters.
Deleted recipes and meals are omitted. Missing recipes return 404, invalid
queries 422, and an unreadable or incompatible database 503.

Preparation and cooking times are returned as `*_raw` values until their unit
has been confirmed against Recipe Keeper's UI. Ingredient text and serving text
are preserved. Photos and account credentials are not exposed.

## Shopping-list behavior

Recipe Keeper's meal entries have titles rather than recipe foreign keys.
The bridge matches complete titles case-insensitively. One match is `matched`;
multiple matches are `ambiguous`; no match is `unmatched`. It never chooses a
recipe from duplicate candidates. A renamed recipe can leave a meal unmatched.

The shopping preview is a starting point for a consumer, not a shopping-list
writer. It returns raw nonempty ingredient lines, one entry per meal and line,
without combining quantities. Pantry rules mark staples unselected in the API's
preview while retaining them in `items`. Recognizable section
headings, linked recipes, empty ingredient lists, unresolved titles, and serving
scales other than numeric `1` generate issues and exclude that meal's lines.
Missing or unrecognized scales also require review. Other free-form ingredient
conventions may need manual handling; `ready` only means these checks passed.

When any meal has an issue, `ready` is false even though unaffected meals can
still appear in `items`. A consumer should check `ready` before automatically
adding anything. Use `/v1/meal-plan` to see the omitted recipes and scales.

Each line has a deterministic `key` based on meal ID, recipe ID, line position,
and ingredient text. A consumer must persist successfully imported keys to
prevent adding them again on each poll. Identical ingredients from two meals
remain separate entries. Editing or reordering lines can change keys, so a
consumer also needs an explicit policy for changed or removed meals. The bridge
does not claim end-to-end exactly-once shopping-list updates.

## Deployment and Home Assistant

See [MacBook deployment](docs/deployment.md) and
[Home Assistant connection plan](docs/home-assistant.md).

Cloud mode instead follows [cloud deployment](docs/cloud-deployment.md) and
supports the included user systemd unit on Linux.

For database mode, the MacBook must be awake and reachable. Recipe Keeper must sync changes from
your other devices before they appear here. The bridge reads on each request,
so no restart is needed after local data changes. Database modification time
does **not** prove cloud sync is current; `cloud_sync_verified` stays false in
database mode. Cloud mode reports its last successful sync explicitly and does
not require the MacBook to be available.

## Development

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Tests use synthetic SQLite databases, including WAL mode, never personal data.
`.env`, databases, copied data, logs, virtual environments, and generated launchd
files are excluded from Git. Keep `uv.lock` in the eventual repository.

See [implementation notes](docs/database-notes.md) for schema observations and
the limits of compatibility with future Recipe Keeper versions.
