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
Core development environment's Python. That initial probe was not a custom
component; see the later native integration validation below. The repository
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
[cloud editing](cloud-editing.md). HACS packaging and HA options/actions were
subsequently added to this branch; production VM installation remains separate.

## Native custom integration, 2026-10-20

`feature/two-way-sync` now contains the installable `recipe_keeper` custom
integration, an administrator options flow, response actions, meal calendar,
aggregate/diagnostic entities, and a bundled recipe/shopping/editing panel.
The target VM was inspected read-only: Core **2026.10.2**, Python 3.14.6,
HAOS 18.3 amd64, HACS 2.0.5. No production VM installation or list changes were made.

The isolated integration suite uses the actual Core 2026.10.2 package and its
matching frontend 20260930.4, with Python 3.14.8 on this Mac. Results:

| Check | Result |
| --- | --- |
| API/shared client regression suite | 176 passed |
| HA integration tests through real setup/actions | 26 passed |
| JavaScript editor regression checks | 4 passed |
| Ruff lint/format and bundled-client provenance | Passed |
| Local hassfest metadata validation | 1 valid integration, 0 invalid |
| Built manual HA ZIP | 28 runtime files, no credentials/cache/test files |
| Independent client wheel import without FastAPI installed | Passed |
| Native aiohttp fresh download and incremental sync | 72 recipes, 55 meals, fresh cache |
| Uploads from native validation | 0 |
| Review subagent | Approved after iterative fixes |

HA coverage includes user setup, duplicate identity, rejected credentials,
connection failure, reauthentication, opt-in options, all six CRUD action
adapters and their default-off gate, non-admin rejection, read actions,
diagnostics redaction, local-day calendar boundaries, cached offline startup,
first-sync retry/cleanup, real frontend registration/unload, and import recovery.
All mutation tests in this new suite use synthetic records and to-do handlers.
Earlier disposable-only cloud CRUD validation remains recorded above.

The importer regression cases reproduce failures before its first disk commit,
before an add, and after an add; lost responses/cancellation; corrupt ledger and
shutdown rejection; and unload followed by a replacement importer. Strict private
atomic writes propagate errors, pending markers precede additions, and terminal
completion requires every planned key to be accounted for. Uncertain calls are
never replayed automatically; unattempted keys remain visible.

Review also found and fixed asynchronous panel account/editor races, validation
errors leaving orphan UUIDs, stale editing controls after Configure, a browser
collision with HTMLElement.editContext, and a policy change leaving an unsent
prepared edit permanently held. Tests establish that a provably unsent operation
can terminate as cancelled, while a request that may have been sent (including
before 401 renewal) retains read-back verification. The shared source and generated
HA bundle are checked byte-for-byte.

Browser smoke tests used a synthetic meal, recipe, pantry line, and in-memory
shopping handler. They verified recipe contents, form validation/correction,
successful synthetic editing, and importing three checked ingredients with olive
oil excluded. The screenshot in the installation guide shows synthetic data.
No real recipe or meal was created, changed, or deleted during this native HACS work.

The ZIP and independent wheel/source archive build locally under ignored `dist/`.
The GitHub workflow repeats Python/HA/JS checks, hassfest, and packaging. HACS's
remote distribution check is conditional on a public repository; it cannot verify
private installation support because HACS does not support private repositories.
Production installation, a public release/license decision, and testing that
release through HACS itself remain user-controlled deployment steps.


## Sync export and public repository checks — 2026-10-20

Version 0.2.1 adds the authenticated API `/v1/sync-snapshot`, native HA
`get_sync_snapshot` response action, and async client facade. Its full-account
scope includes active recipes/meals across all dates, exact edit revisions,
opaque last-modified strings, cloud tombstones, source account namespace, and
unresolved outbound operations. A content-derived snapshot ID is stable across
unchanged polls. Export reads use one SQLite transaction and exclude photos,
credentials, unknown fields, and private operation payloads/baselines.

Validation: **188 API/client tests**, **27 HA tests**, **4 JavaScript tests**, Ruff,
bundle provenance, and hassfest passed. Added cases cover authentication, exact
revision compatibility, all-date and unplanned-recipe coverage, tombstones without
dates, title ambiguity, concurrent cache updates during export, unresolved edits,
incomplete-cache rejection, the no-network async facade, and HA freshness handling.
A read-only export of the existing real cache returned **72 recipes, 55 meals,
12 recipe tombstones and 7 meal tombstones**, with completeness true and stale true
because that saved cache had not recently polled. No cloud requests or uploads
were needed for this check. No actual recipes or meals were changed.

The owner made the repository public. README now includes a HACS repository button
and installation steps; a default-branch merge or published release is still needed
for HACS to install the feature-branch component. Repository description/topics
were added for HACS discovery. Current build artifacts are the manual 0.2.1 ZIP and
independent client wheel/source distribution under ignored `dist/`.

See [Skylight compatibility and remaining upstream work](skylight-sync.md) and
[security audit results](security-audit.md). No Skylight live sync has been started.
