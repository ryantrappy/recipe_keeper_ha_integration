# Validation record

Validated locally on 2026-10-19 with Python 3.14.8 and Recipe Keeper 3.46.1.

- 37 automated tests passed against synthetic databases.
- Ruff lint and format checks passed.
- Generated launchd plist passed `plutil -lint`; it was not installed.
- The diagnostic command read the live local database successfully.
- All 72 active recipes were fetched through the API successfully.
- The week 2026-10-19 through 2026-10-25 returned one planned meal and 14
  ingredient preview lines, with no preview issues in that range.
- An actual loopback HTTP server returned 200 for authenticated status, search,
  meal-plan, and shopping-preview requests and 401 without authentication.
- The live database SHA-256 was unchanged before and after the in-process
  read-only API checks.
- The temporary HTTP server was stopped after verification.

Tests cover SQLite write rejection, snapshot consistency, WAL visibility,
fresh reads, deleted entries, literal search, pagination, ambiguous and missing
recipe matches, date boundaries, unsafe or unknown serving scales, ingredient
headings, linked recipes, missing databases, schema drift, authentication, and
configuration file permissions and overwrite protection.

The installed Starlette test client emits a deprecation warning for its use of
httpx. This did not cause test failures; production serving does not use the
test client.

Not yet validated: service permissions and sleep behavior on the destination
MacBook, cloud sync freshness, remote networking from Home Assistant, and writes
to a shopping-list provider. No background agent or Home Assistant configuration
was installed or changed.

## Cloud investigation

The subsequent [cloud sync probe](cloud-investigation.md) matched all 72 active
recipe IDs and 55 active meal IDs. The selected text fields had no mismatches.
A full cloud response was 9,941,570 bytes; an immediate incremental check was
700 bytes with no changes. Both requests uploaded empty entity arrays and
received no upload acknowledgements. This initial comparison used the Mac's
cached session.

The expanded suite passes 94 tests, including synthetic cloud pagination,
exact cursor preservation, upload exclusion, response validation, safe
summaries, and read-only comparison. Existing database API tests still pass.
Fresh login tests cover form encoding, expiration metadata, hidden-input handling,
error-body redaction, absence of automatic login retries, and use of the entered
credentials rather than cached sessions.

The account holder subsequently ran `--login` successfully and shared the
sanitized result. A fresh token retrieved 72 active recipes and 55 active meals;
the incremental check returned no changes and no entities were uploaded. Reported
`expires_in_seconds` was 0, issue/expiration timestamps were null, and no refresh
token was returned. Token lifetime is therefore unknown; coexistence with
existing app sessions still needs validation. No credentials
were requested or supplied in chat.

## Persistent cloud backend

- Independently signed in using the locally configured credentials file and
  populated the separate cache with 72 recipes and 55 meals.
- All 72 recipe API models and all 55 meal API models matched the original
  database backend, including recipe contents and planner dates.
- Saved exact per-section cursors and successfully used them on a later sync.
- Verified the app's original database was unchanged by cache creation and reads.
- Forced a real HTTP 401 with an intentionally invalid token. The backend signed
  in exactly once and completed the same incremental request successfully.
- Simulated a network outage after caching. The cache survived reopening and
  served all 72 recipes with a stale header and recorded failure category.
- Started the actual cloud HTTP service; its background sync recovered the cache
  from the simulated failure and its authenticated endpoints returned fresh data.
- Stopped the temporary server after verification; no background service was installed.

Additional automated coverage includes persistence across cache reopen, account
isolation, refusal to modify foreign databases, atomic rollback of partial or
invalid downloads, tombstones, initial-cache 503 behavior, stale responses,
background-service lifecycle, credential file permissions, and retry/backoff.

Still pending: installation and execution on trappserv.er, Home Assistant's
connection and shopping-list writes, and observation of an actual token expiry
(as distinct from the live forced-401 test).

## Native Home Assistant feasibility

An isolated Home Assistant development runtime (`2026.11.0.dev0`, Python 3.14.8)
successfully signed in and downloaded the account's data through HA's own shared
aiohttp session, without the FastAPI service or Mac app files. Its private,
atomic `Store` snapshot reloaded identically. Temporary HA response actions
returned 72 search results and full recipe contents. All 55 meals were converted
to native calendar event objects. No entity updates were uploaded to the cloud
and no existing home configuration or shopping lists were changed.

The repeatable probe is `scripts/probe_home_assistant_runtime.py`, run with the
Core development environment's Python. It is not a custom component. HACS
packaging, actual calendar entity installation, target stable HA compatibility,
and live VM installation remain to be implemented and tested. The repository
was observed to be private, which blocks HACS distribution under its current
published requirements. See [the feasibility report](hacs-feasibility.md).

## Ingredient selection and pantry defaults

At that stage, the suite passed 117 tests. Selection tests cover individual choices,
pantry overrides, conservative olive-oil matching, an empty selection, changed
keys, retained review issues, and read-only API operation. A cached live preview
retained all 14 ingredient lines, selected 13 by default, and left its olive oil
line unchecked. Explicitly selecting that pantry line succeeded. The user's
private `.env` now includes olive oil as a staple; source recipes were unchanged.

## Two-way cloud editing

The feature branch implements default-off editing for the standalone cloud API.
An independent reviewer checked the full implementation and approved after
fixes for malformed upload responses, interrupted HTTP replies, and explicit
proof that live preservation fields were seeded. The suite now passes 164
tests; Ruff lint/format and documentation link checks also pass.

Synthetic tests cover full raw record retention, photo/unknown-field preservation,
schema 1 migration with forced full refresh, stale revisions and deleted records,
durable baselines/intents, idempotent operation IDs, interrupted responses before
and after commit, malformed acknowledgements, verification outages, restart,
default/off gates, read-only reconciliation, authentication, API validation, and
the live harness's refusal to edit original or unowned IDs.

Live validation used only newly generated disposable IDs. Private full snapshots
were saved before writes, and ownership registries were persisted before create
uploads. The final harness run verified:

| Check | Result |
| --- | --- |
| Meal create, edit, independent read-back, delete | Passed |
| Recipe create, edit, independent read-back, delete | Passed |
| Seeded photo, calories, protein, and category fields present before edit | Passed |
| Untouched recipe fields preserved after a notes edit | Passed |
| Upload with stale `LastModified`, on disposable records only | Accepted for recipes and meals |
| Original 72 active recipes and 55 active meals | All unchanged |
| Every original full record, including original tombstones and photos | Matched the initial pre-test snapshot |
| Active disposable records remaining across all tracked test runs | 0 |

Some live attempts encountered uncertain upload/verification outcomes. They
were held without replaying uploads; independent reads and owned-record cleanup
resolved the test records. The harness now permits up to two explicit read-only
reconciliations before declaring a test incomplete. Its final run passed all
checks, including the stronger seed assertions. No existing recipe or meal was
edited, deleted, or restored by these tests. Real app UI rendering was not part
of this validation.

The server accepts stale writes, so the client can reject a stale preflight
revision but cannot guarantee against another device changing the record
between refresh and upload. This limitation is documented in
[cloud editing](cloud-editing.md). HACS packaging, HA options/actions, and VM
installation remain separate from this API branch.
