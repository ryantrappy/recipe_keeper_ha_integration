# Cloud sync feasibility

**Fresh login and direct cloud downloads work for this account.** Full sync
downloads and cursor-based incremental checks succeeded on 2026-10-19, including
a test run by the account holder with newly entered credentials. Cloud access is the
stronger long-term candidate for running on trappserv.er without an always-on
MacBook. The service now has an optional persistent cloud backend; see
[cloud deployment](cloud-deployment.md). Database mode remains available. The
experimental probe remains useful for independent diagnostics.

## Verified results

Inspected the installed Recipe Keeper for macOS 3.46.1 (build 144), including
`RecipeKeeper.Core.dll` and `RecipeKeeper.Core.Interface.dll`. Then made download
requests using the app's existing signed-in bearer token, held only in memory.
No passwords were decrypted in this initial cached-session test, and the app's
preferences and database were not written by the probe. The account holder
subsequently validated fresh login using the interactive command below.

| Check | Result |
| --- | --- |
| Active recipes downloaded | 72 |
| Active meals downloaded | 55 |
| Deleted recipe records received | 9 |
| Deleted meal records received | 2 |
| Active IDs missing from either local or cloud set | 0 |
| Recipe text mismatches | 0 |
| Meal text mismatches | 0 |
| Full response size | 9,941,570 bytes |
| Immediate incremental response | 700 bytes; no recipe or meal changes |
| Entities uploaded by the probe | 0 |

Recipe comparison checked title, ingredients, directions, and serving text.
Meal comparison checked title, meal type name, scale, and notes. This does not
claim a field-by-field comparison of every field, photo, or meal timestamp.
Dates and additional fields were present in the cloud response but not all were
validated against local storage. Counts describe the account at test time.

The full response includes base64 photo fields, which explain much of its size.
The probe discards photos after decoding the response and never saves recipes,
meal contents, or tokens. It only prints aggregate results. The server still
transmits the photos; a client-side filter does not reduce that initial transfer.

## Observed protocol

Base address: `https://recipekeeper.azurewebsites.net/`

| Path | App behavior |
| --- | --- |
| `POST /token` | Form login using `username`, `password`, `grant_type=password` |
| `POST /api/sync` | JSON synchronization with a bearer token |
| `POST /api/syncnotification` | The app's long-poll notification mechanism; not tested here |

The successful probe used `Authorization: Bearer <session token>`,
`X-RK-APIVersion: 10`, JSON content type, and an identifying research user agent.
It did not need the app's device, OS, analytics, or purchase-receipt headers for
the tested downloads. That is an observation about this account/session, not a
promise that those headers will never be required.

A full recipe-and-meal request looks like this; it contains no local updates:

```json
{
  "RecipeSyncEntityRequest": {
    "MaxLastModified": "1601-01-01T00:00:00Z",
    "SyncEntities": [],
    "LocalChangesIds": []
  },
  "MealSyncEntityRequest": {
    "MaxLastModified": "1601-01-01T00:00:00Z",
    "SyncEntities": [],
    "LocalChangesIds": []
  }
}
```

The initial date comes from the app's `SyncRequest.MinLastSyncMaxLastModified`,
initialized with `.NET DateTime.FromFileTimeUtc(0)`.

Each requested section returns:

```json
{
  "UpdatedLocalItems": [],
  "UpdatedServerItems": [],
  "HasMoreUpdatedServerItems": false,
  "MaxLastModified": "<server cursor>"
}
```

`UpdatedServerItems` contains records to download, including `IsDeleted`
tombstones. `UpdatedLocalItems` acknowledges local uploads; it remained empty
in these probes. Preserve each section's cursor **as the original string**:
observed timestamps have seven fractional digits (100 ns precision), more than
Python's normal datetime precision. Send it back unchanged for incremental reads.

The server exposes separate recipe and meal cursors. Continue each section while
`HasMoreUpdatedServerItems` is true. The prototype supports pagination with a
bounded request count, but live pagination was not exercised because the entire
account fit in one page. Pagination behavior is covered with synthetic responses.

Recipe records contain ingredients, directions, source, serving text, categories,
linked recipe IDs, nutrition, and photos. Meal records contain the same title,
date, type, scale, and notes concepts used by the database bridge. They still
match recipes by title, so cloud access does not resolve duplicate or renamed
recipe ambiguity. The app's code uses sync to obtain records; search can run
against a locally cached collection, just as it does now against SQLite.

## Authentication: known and still unverified

The app's `WebServiceClient.SignIn` submits the password grant to `/token` and
reads an access token from the JSON response. On a sync 401, `CallSyncService`
calls `SignInWithCurrentCredentials` and retries. That is evidence for signing
in again with credentials rather than relying on a refresh token in this path.

For this investigation only, the existing `AccountBearerToken` was read from
the Mac app's preferences. The code inspection showed that this value is stored
separately from the encrypted email/password fields. The probe does not print or
persist it and does not decrypt those other fields.

The account holder ran the interactive fresh-login command successfully. Its
new token downloaded 72 active recipes and 55 active meals, and the immediate
incremental check returned no changes. This verifies that access does not depend
on reusing the Mac app's cached session.

The reported authentication metadata was `expires_in: 0`, with no parsed issue
or expiration timestamp and no refresh token returned. The token was accepted
for both sync requests, so zero does not establish that it expired immediately.
It also does not prove the token never expires. Treat the lifetime as unknown.
Inspection of the app's JSON property mappings confirmed the probe's field names
(`expires_in`, `.issued`, `.expires`); no alternate expiry names were found there.

Token lifetime, revocation behavior, existing-app session coexistence, and
expiration-triggered reauthentication remain unverified live. No refresh-token flow was
established. The standalone probe stops on 401. The service's cloud backend
implements one fresh sign-in and one retry of that same sync, then records an
authentication failure if that also fails. Cursors and cached data are preserved
during renewal. Synthetic tests cover the retry limit. A live test with an
intentionally invalid bearer token also received 401, signed in once, and
completed the retry using saved cursors. Waiting for a real token to expire is
still unverified.

A production cloud backend should own its credentials independently of the Mac,
store them through the deployment's secret mechanism, handle 401 and backoff,
and avoid changing the Mac app's stored account state. A one-time cached token
proves access but does not establish an unattended service lifecycle.

## Reproduce the probe

### Fresh authentication test

Run this in a local interactive Terminal, on macOS or Linux:

```sh
cd /Users/ryantrapp/Code/recipe_assistant_api
uv run python -m recipe_assistant.cloud_probe --login
```

Enter the email and password for your **Recipe Keeper account** when prompted.
Both inputs are hidden. This flow does not read the Mac's cached token or reuse
`RECIPE_KEEPER_CLOUD_TOKEN`. It makes one login attempt with the password grant,
then tests full and incremental downloads with the new token. Failed logins are
not automatically retried. HTTP errors are reported without server response bodies.

The JSON summary reports whether a token was received, its reported lifetime
and issue/expiry timestamps when available, whether a refresh token was returned,
and the download results. Missing expiration metadata appears as `null`. The
email, password, access token, refresh token value, and recipe text are excluded
from output. Credentials and tokens remain in memory for this process and are
not saved. The command refuses to proceed if it cannot hide credential input.

Optional local database comparison:

```sh
uv run python -m recipe_assistant.cloud_probe --login --compare-database
```

This tests issuing and using a fresh token. It does not wait for the token to
expire or prove automatic renewal. Check that your Recipe Keeper apps can still
sync afterward to verify session coexistence.

### Existing Mac session test

On this Mac, with Recipe Keeper signed in and the necessary Full Disk Access:

```sh
cd /Users/ryantrapp/Code/recipe_assistant_api
uv run python -m recipe_assistant.cloud_probe --mac-session --compare-database
```

`--mac-session` explicitly authorizes reading the cached app session. It reads
the current user's preferences path and never exports the token to another file.
Without that flag, the probe requires `RECIPE_KEEPER_CLOUD_TOKEN` supplied by the
calling environment. Do not paste a real token into Git, a command argument,
an issue, or chat. This token is distinct from the local bridge API token.

`--compare-database /path/RecipeKeeper.db` can select a different comparison
database. Omitting `--compare-database` makes the download independent of SQLite.
The actual destination Linux runtime has not been tested yet.

The probe only contacts the fixed HTTPS sync URL, refuses redirects, uploads
empty entity arrays, omits account-change operations, rejects unexpected upload
acknowledgements, and emits summary counts. It does not retain sync cursors or a
recipe cache between runs; rerunning it deliberately performs another full
download. It is for investigation, not frequent scheduled polling. Its token is
an existing account credential and is not restricted to read-only permissions
by this client; download-only behavior comes from the request construction.

## Compare with the database bridge

| Concern | Local database | Direct cloud |
| --- | --- | --- |
| Runtime | Mac with Recipe Keeper data | Potentially trappserv.er/Linux; destination test pending |
| Mac availability | Mac must be reachable and awake | Not needed after independent authentication is implemented |
| Freshness | Waits for Mac app sync | Reads changes already synced to the cloud |
| Authentication | Local file permission plus bridge token | Recipe Keeper session/credentials plus bridge token if served |
| Offline use | Can read local synced recipes | Needs internet; a production cache could serve older data |
| Incremental data | SQLite handles storage | Client must merge pages, cursors, and deletions |
| Photos | Already stored locally | Large initial download; deltas can be small |
| Compatibility | Undocumented database schema | Undocumented service protocol and authentication |
| Serving scales and title ambiguity | Existing review rules | Same review rules still needed |
| Current readiness | Working authenticated API | Persistent cache and background sync implemented; Linux deployment pending |

The implemented cloud backend persists data and exact cursors together, applies
tombstones, retries once after reauthentication, polls with backoff, and serves
the existing recipe/meal response models from its own SQLite cache. The database
backend remains a fallback. Linux deployment and observation of an actual expired
token remain follow-up checks; successful fresh login has already been verified.

No documented public API or developer support commitment was established by
this work. The result is evidence of technical feasibility for the tested
private protocol, not vendor-supported integration access.
