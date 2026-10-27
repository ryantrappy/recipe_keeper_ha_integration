# Cloud editing

The cloud backend supports explicit recipe and meal create, update, and delete
operations. Editing is **off by default**. Database mode always stays read-only.
This is the standalone API implementation; an installable HACS integration and
its Configure option remain separate work.

## Enable editing

Set this in the service's private `.env` and restart the service:

```dotenv
RECIPE_ASSISTANT_BACKEND=cloud
RECIPE_ASSISTANT_ALLOW_EDITS=true
```

Only `true` and `false` are accepted, case-insensitively. An absent value means
`false`. No HTTP request can enable editing. Both the API and upload transport
enforce the setting. `/v1/status` reports `allow_edits` and operation counts.
Turning it off stops future writes after restart; an already submitted request
may have committed. Neither restart nor enabling editing replays held operations
or uploads the cached collection. The account token has no established read-only
scope; this flag is enforced by our client.

## Editing endpoints

Every endpoint requires the existing local API bearer token.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/recipes/{id}/editing` | Editable fields and complete-record revision |
| `GET /v1/meals/{id}/editing` | Editable fields and complete-record revision |
| `POST /v1/recipes` | Create a recipe |
| `POST /v1/meals` | Create a meal |
| `PATCH /v1/recipes/{id}` | Edit selected recipe fields |
| `PATCH /v1/meals/{id}` | Edit selected meal fields |
| `DELETE /v1/recipes/{id}` | Delete using the current revision |
| `DELETE /v1/meals/{id}` | Delete using the current revision |
| `GET /v1/edit-operations/{operation_id}` | Inspect an operation without uploading |
| `POST /v1/edit-operations/{operation_id}/reconcile` | Download and verify without uploading |

Recipe fields are `title`, `serving_size`, `source`, `ingredients`, `directions`,
`notes`, `favourite`, `categories`, `rating`, `preparation_time_raw`, and
`cooking_time_raw`. Meal fields are `title`, `date`, `meal_type`,
`meal_type_code`, `scale`, `notes`, `position`, `meal_time_raw`, and `colour_raw`.
Fields marked `raw` preserve app values; their UI units are not inferred.
Dates use `YYYY-MM-DD`, with no UTC conversion. Scales are positive decimal
strings; existing shopping-preview review rules still apply to scales other
than `1`. Photos, nutrition, sharing information, linked IDs, and other metadata
are preserved on update but are not editable through these endpoints.

Creating a recipe requires a title. Creating a meal requires a title and date;
defaults are meal type name `Dinner`, legacy type code `0`, scale `1`, and
position `0`. Supply a type name/code from an existing meal if needed.

Every mutation requires a caller-generated UUID `operation_id`. Reuse that UUID
when retrying **the exact same request**. It identifies the durable operation;
creates receive a separate generated entity ID. Reusing an operation ID with
different content returns `409`. A retry returns the recorded outcome and never
resends an upload, including when its outcome is uncertain.

Example create body:

```json
{
  "operation_id": "79aeb0c1-7bed-4efc-aeef-97c79435b715",
  "changes": {
    "title": "Example dinner",
    "date": "2026-11-20",
    "meal_type": "Dinner"
  }
}
```

For an update, fetch `/editing`, retain its `revision`, and submit only the
fields you want to change:

```json
{
  "operation_id": "c75a30cb-9c39-4054-b647-d33645231c9d",
  "revision": "<64-character revision from the editing view>",
  "changes": {"notes": "Prepare ahead"}
}
```

A delete body contains `operation_id` and `revision`, with no `changes`.
Empty updates, null field values, and unsupported fields are rejected. Recipe
renames do not rename meals: Recipe Keeper associates them by title, so review
resulting unmatched meals.

## Outcomes and conflicts

An edit refreshes cloud data, checks the caller's revision against the complete
record, and saves its original record and intended payload before uploading.
Downloads and edits share a cross-process cache lock. Stale revisions return
`409` before upload; missing/deleted records return `404`. Default/off mode
returns `403`, a failed preflight returns `503`, and a busy sync returns `409`.
Retry a busy request later using the same operation ID.

**The tested server accepts stale `LastModified` values.** Preflight protects
against changes it finds, but another device can write between our refresh and
upload. No atomic server conditional-write guarantee was established.
Concurrent edits can overwrite one another; automatic conflict resolution is
not provided. Coordinate editing with other Recipe Keeper clients while using
this experimental feature.

After upload, a separate full download verifies the intended record, including
untouched fields. The cache receives the download and cursors only after every
page succeeds. Upload response cursors never skip this verification. Full
downloads transfer photos too (about 10 MB for the tested account), so edits
transfer more data than normal incremental polling.

Responses contain identifiers, action, state, sanitized error code, and creation
time, without saved record contents or credentials.

| State | Meaning |
| --- | --- |
| `confirmed` (`200`) | Independent download matches the intended result |
| `uncertain` (`202`) | May have committed, or verification failed; never automatically replayed |
| `conflict` (`409`) | Remote record differs from both baseline and intended result |
| `prepared` / `sending` (`202`) | Interrupted persisted work; inspect/reconcile it |
| `cancelled` (`200`) | Journal proves no upload began; a new operation may be submitted after review |

Reconciliation works with editing off and only downloads. It confirms matching
results or reports conflicts. If the record still equals the baseline, an
uncertain operation stays held: absence of a change does not prove a timed-out
request can no longer commit. Unresolved work blocks another edit to that
entity. There is no automatic merge, retry, or force-clear endpoint in this
version.

## Storage, backup, and testing

Cache schema 2 retains complete records, photos, and exact timestamp strings in
private `RawRecords`. Schema 1 migrates and requires a full refresh before
editing; discarded fields cannot be reconstructed from its reader projection.
`EditOperations` stores each original record and intended payload. Keep the
cache's additive `AutomationRequests` table too: version 0.3.0 uses it to preserve
random recipe selections and URL-import contents across retries and restarts. Keep the
cache private and backed up. This journal is recovery evidence, not automatic
undo or an account export. Back up live SQLite through its backup API rather
than copying its main file without the WAL.

Unit tests use synthetic records and credentials. The opt-in live harness is:

```sh
uv run python scripts/validate_cloud_edits.py --run-live-test
```

Without the flag, it makes no requests. With it, it saves a private full cloud
snapshot under Git-ignored `local-data/two-way-validation/<run-id>/`, creates
uniquely titled disposable records, verifies create/update/delete and seeded
photo/nutrition preservation, and measures stale-write behavior. Its upload
guard refuses every original snapshot ID and every record not registered as
created by that invocation. Cleanup only deletes owned records, then compares
every pre-existing record with the backup. Files use mode `0600` in a private
directory. If cleanup fails, retain those files and inspect the owned IDs
before another live test.

A recovered `prepared` operation is cancelled without uploading because the engine
commits `sending` before entering the upload path. Disabling editing before a send
also cancels the operation. If a send may have occurred (including before 401 renewal),
it stays uncertain and requires download-only verification. The native HA integration
uses the same engine; see [its setup guide](hacs-installation.md).
