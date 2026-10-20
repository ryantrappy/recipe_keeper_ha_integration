# Optional two-way sync

Two-way recipe and meal-plan sync is implemented in the native HA integration and standalone cloud API,
with editing off by default. See [cloud editing](cloud-editing.md) for the
setting, endpoints, recovery behavior, and limitations. Create, update, and
delete have been verified using disposable records only. The original design
and binary findings follow; HA options/actions and the panel are now implemented.
See [HA installation](hacs-installation.md).

This can fit the [native Home Assistant architecture](hacs-feasibility.md) and
run inside the HA VM. It does not introduce a requirement for a Docker/API
service. The local Recipe Keeper SQLite database should remain read-only.

## Evidence from the installed app

Inspected macOS Recipe Keeper 3.46.1, build 144, on 2026-10-19. These findings
come from `RecipeKeeper.Core.dll` and `RecipeKeeper.Core.Interface.dll`, not a
documented public API or a successful write experiment.

| Code | Observed behavior |
| --- | --- |
| `EntitySyncClient.GetRequest` | Copies changed entities into `SyncEntities`; includes all locally changed IDs in `LocalChangesIds` |
| `Meal.CopyToSyncEntity` | Copies the base fields plus date, meal type, title, position, type name, time, colour, scale, and notes |
| `Recipe.CopyToSyncEntity` | Copies recipe text, groups, rating, favourite status, photos, sharing ID, nutrition, linked IDs, and added date |
| `EntityBase.CopyToSyncEntityBase` | Copies `Id`, `IsDeleted`, and `LastModified` |
| `UpdatedItem` | Upload acknowledgement contains `Id` and `LastModified` |
| `EntitySyncClient.ProcessResponse` | Applies acknowledgement timestamps, clearing dirty state only if the local version still matches the submitted version; deletes acknowledged local tombstones |
| Download processing | Skips IDs in the local change set and skips locally dirty entities |

The local `VersionNo` check prevents an acknowledgement for an older edit from
clearing a newer local edit. It does **not** establish server-side conditional
updates or conflict rejection. The inspected base wire record does not include
`VersionNo`. Live tests on disposable recipes and meals found the server accepts
stale `LastModified` values. Preflight checks cannot eliminate concurrent-write
races. Create defaults, generated UUIDs, uploads, and tombstone deletion work in
the tested account. Automatic replay of writes remains unsupported.

## Proposed editing flag

Use one account-specific boolean, `allow_edits`, defaulting to `false`. In HA,
expose it as **Allow editing Recipe Keeper** in the integration's Configure
options, using the normal administrator configuration UI. It should not be an
ordinary switch that automations can enable. HA supports persisted integration
options through an [options flow](https://contributors.home-assistant.io/core/integrations/options-flow/).

| Value | Behavior |
| --- | --- |
| `false` or absent | Continue downloads; reject Recipe Keeper create/update/delete operations; send empty upload arrays |
| `true` | Permit explicit, supported edit operations; keep normal download sync running |

Enabling the option must not upload cached records automatically. Only an
explicit edit creates outbound work. Enforce the flag both at the integration
action boundary and in the cloud client's upload path. Callers must not be able
to override it with a per-request argument. If the standalone API gains writes,
give it the equivalent startup setting, `RECIPE_ASSISTANT_ALLOW_EDITS=false`,
and keep database mode read-only regardless of that setting.

Turning editing off stops new edits and unsent uploads. Hold pending changes
for explicit review rather than silently sending them when editing is enabled
again. A request already sent may have committed; continue download-only
reconciliation to determine its outcome. Report editing state, pending changes,
conflicts, and uncertain outcomes without exposing credentials or recipe text
in diagnostics.

This is a client-enforced policy. Our existing bearer token has no established
read-only scope, so the flag is not a separate server permission.

## Implementation requirements

The standalone API and native HA adapters implement these requirements.

1. **Preserve complete source records.** The current probe discards photos and
   the cache stores only selected recipe/meal fields. It also drops per-record
   `LastModified`. Add a versioned, private store of complete server records and
   exact timestamp strings before permitting edits. Refresh the complete
   baseline; the current cache cannot reconstruct discarded fields. Preserve
   fields outside the edit schema, including photos, nutrition, groups, sharing
   metadata, meal colour/type, and unknown fields. Verify whether the server
   replaces whole records or supports partial updates; never assume omission
   means preservation.
2. **Build an explicit upload operation.** Construct the app's sync DTO from a
   complete baseline plus validated edits, track `LocalChangesIds`, and parse
   upload acknowledgements separately from downloads. Keep the existing
   download-only request builder and its acknowledgement guard. Verify create
   IDs/defaults and tombstone semantics before enabling those operations.
3. **Persist outbound state.** Record the entity ID, baseline, intended change,
   local revision, and operation state before sending. Serialize uploads with
   downloads. Acknowledgement of revision N must not clear revision N+1. Save
   acknowledgements, cursors, inbound changes, and outbound state consistently
   across restarts. Start with online edits; offline editing is an additional
   feature, not necessary for the first release.
4. **Detect conflicts and uncertain results.** Refresh before an edit and
   compare the caller's baseline with the latest complete record. Hold an edit
   if the record changed or was deleted. Do not silently recreate deleted
   records or choose a last writer. This preflight cannot eliminate a race
   with another device; verify server conditional-write behavior before making
   any stronger guarantee. After a timeout, reconcile by ID and content before
   retrying; stable IDs alone do not prove retry safety. Preserve unresolved
   operations for review instead of blindly replaying them.
5. **Verify the result.** Validate acknowledgements and independently download
   the committed record to confirm it and establish the next baseline. A sync
   response may omit our own uploaded IDs, so an empty delta is not enough.
   Use a separate full download if needed until a narrower verification path
   is established, without rewinding the normal reader's cursors.

Recipe edits need an additional title policy. Existing meal records associate
with recipes by title. Renaming a recipe can leave meal entries unmatched;
do not cascade changes to meals without an explicit operation and validation.

## Home Assistant exposure

Start with explicit meal actions for scheduling, moving a meal, changing notes
or scale, and removing a meal. Then add recipe create/update/delete actions
after complete-record preservation has passed live tests. Unsupported operations
stay unavailable even when `allow_edits` is enabled.

HA calendars have optional create, update, and delete methods. They could expose
the meal edits after their mapping is verified, using the meal ID as the stable
calendar UID and preserving calendar dates. Meal type, scale, ordering, and
Recipe Keeper's lack of an established recurrence model need deliberate
handling; explicit meal actions are sufficient for the first version.
[Calendar entity interface](https://contributors.home-assistant.io/core/entities/calendar/).

All actions must reject writes while editing is off, even if an old automation
still calls them. Calendar editing features should only be advertised for
operations supported by the client and enabled for that account. Refresh HA
state after confirmed writes, and return a distinct pending/conflict result
when completion is uncertain.

Adding selected ingredients to an existing **Home Assistant shopping list** is
separate from editing Recipe Keeper. Pantry exclusions such as olive oil should
remain preview/import preferences and need no recipe edits. Bidirectional sync
with **Recipe Keeper's own shopping lists** would require investigating those
additional entities; recipe and meal sync does not establish that capability.

## First live validation

Use disposable records chosen for the write test, with a backup/export before
testing. Keep ordinary recipes and meal entries outside the experiment.

1. Create a uniquely named test meal, download it independently, and confirm
   it appears correctly in Recipe Keeper after the app syncs.
2. Change its date, notes, scale, and meal type one at a time; confirm all
   untouched fields and the ID survive. Delete it and verify the tombstone.
3. Test a disposable recipe with a photo, nutrition, groups, and linked data.
   Edit one text field and verify every untouched field survives. Validate
   recipe creation, deletion, and title-matching effects separately.
4. Exercise competing app/integration edits, edit versus delete, edit during
   an upload, missing or partial acknowledgements, timeout after commit, 401,
   rate limiting, and restart with pending work. Use synthetic failures where
   possible; observe real server conflict behavior on disposable records.
5. Prove that default/off mode sends no uploads, including after restart,
   reauthentication, stale action calls, pending work, and disable/re-enable.

The complete-record store, guarded upload path, durable operation journal,
independent verification, API actions, and synthetic tests are implemented.
Disposable-record write tests passed; see the validation record. Native HA adapters
and packaging are implemented. Production VM installation remains a separate step.
