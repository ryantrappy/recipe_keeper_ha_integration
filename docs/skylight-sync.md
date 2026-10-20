# Preparing Recipe Keeper for Skylight meal synchronization

The Recipe Keeper interface now exposes what a separate sync controller needs to
read and change recipes/meals. It does not start synchronization or modify Skylight.
The comparison below uses [MegaTheLEGEND/skylight_calendar](https://github.com/MegaTheLEGEND/skylight_calendar)
commit `979848b9624098812765ba1939098ada1f68eff2`, integration version 3.0.0,
reviewed on 2026-10-20. Its HA domain is **`skylight`**, despite the repository name.

## Recipe Keeper access

Read a complete cached account through either interface:

- HA action **`recipe_keeper.get_sync_snapshot`**, with `config_entry_id` and a
  `response_variable` in an automation.
- Authenticated **`GET /v1/sync-snapshot`** on the optional standalone cloud API.
- Python client **`await client.sync_snapshot()`**.

The export is read-only and does not itself contact either cloud. For current data,
call `recipe_keeper.refresh` first, then inspect the snapshot's freshness. The API
worker normally polls in the background. The local Mac database backend returns
503 for this endpoint rather than claiming cloud revisions/deletion coverage.

```yaml
- action: recipe_keeper.refresh
  data:
    config_entry_id: YOUR_RECIPE_KEEPER_ENTRY_ID
  response_variable: refreshed
- action: recipe_keeper.get_sync_snapshot
  data:
    config_entry_id: YOUR_RECIPE_KEEPER_ENTRY_ID
  response_variable: keeper
```

Snapshot schema version **1** provides:

| Field | Meaning |
| --- | --- |
| `scope` | `all_cached_recipes_and_meals`: all cached dates and the full active cookbook, including unplanned recipes |
| `account_id` | Opaque account namespace; pair it with each record ID |
| `snapshot_id` | Deterministic hash of account, record IDs/revisions, and unresolved operations; an unchanged successful poll keeps this ID |
| `complete` | The cache has a complete raw snapshot and matching projections; independent of freshness |
| `stale`, `last_successful_sync_at` | Whether the last complete snapshot is currently usable and when it last synced |
| `allow_edits` | Current Recipe Keeper write policy; defaults false |
| `recipes` | IDs, exact revisions, original `last_modified`, title, servings, ingredients/lines, directions, notes, source, categories, favourite, rating, durations, and linked-recipe IDs |
| `meals` | IDs, revisions, original `last_modified`, local date, title, meal type, scale, notes, position, raw meal-type/time/colour fields, plus explicit recipe match/candidates and matched recipe contents |
| `deleted_recipes`, `deleted_meals` | Available cloud tombstones: ID, revision, and original `last_modified`; a deleted meal need not retain its old date |
| `unresolved_operations` | Prepared/sending/uncertain edits: operation ID, record type/ID, action, state, error code, creation time; excludes private baselines/payloads |

All data and metadata come from one SQLite read transaction. Revisions are the same
hashes used by `get_editing_view` and guarded update/delete actions. Cloud timestamps
are opaque strings: retain their original subsecond precision rather than rounding
or using them for automatic last-writer-wins conflict decisions. Photos, tokens,
credentials, and arbitrary raw fields are excluded from the export. Response data
still contains your private cookbook: keep exports and automation traces private.

Recipe Keeper meals associate with recipes by title, not a proven persistent recipe
foreign key. Only one case-insensitive title match yields `match: matched`.
`ambiguous` lists all candidate IDs and provides no chosen recipe; `unmatched` can
represent a free-text meal. A sync controller must preserve these cases for review,
not guess a recipe or rewrite a meal's title to force a match.

Existing actions support recipe/meal create, update, and delete, explicit operation
UUIDs, full revision checks, and read-only operation reconciliation. See
[HA installation/actions](hacs-installation.md) and [cloud editing](cloud-editing.md).
Use a new UUID only for a new reviewed intent; never replay an uncertain operation
under another ID. No general calendar-write adapter is necessary for this interface.

## Mapping to the current Skylight integration

| Recipe Keeper | Skylight representation | Limit or decision |
| --- | --- | --- |
| Recipe ID | Recipe `id` / `recipe_id` | Persist an explicit account/frame-scoped ID map; names are not identities |
| Recipe title | Recipe `summary` | Duplicate names must remain distinguishable by mapped IDs |
| Ingredients, directions, notes, source, servings | Recipe `description` | Skylight's exposed recipe API uses one text field; compose labelled sections for export and retain structured fields on the Recipe Keeper side |
| Meal ID + date | Sitting `id` + occurrence date | Recurring Skylight sittings require a sitting-and-occurrence mapping |
| Meal type/name/code | Frame-specific `meal_category_id` | Discover categories and configure a mapping; do not hardcode IDs or equate the two systems' numeric codes |
| Meal scale/order/time/colour/notes | No corresponding fields in `skylight.plan_meal` | Preserve them in the controller's mapping/baseline; do not clear them on reverse sync |
| Cloud tombstone | Mapped counterpart deletion | Only explicit, reviewed deletion of records the controller owns |

The referenced [Skylight API wrapper](https://github.com/MegaTheLEGEND/skylight_calendar/blob/979848b9624098812765ba1939098ada1f68eff2/custom_components/skylight/api.py)
has methods for date-range meal reads, meal categories, all recipes, one recipe,
recipe creation/update/deletion, and meal creation. It does **not** implement meal
sitting update/delete methods at this revision. Those endpoints/semantics must be
investigated before promising moves, replacements, or deletion round trips.

Its [HA services](https://github.com/MegaTheLEGEND/skylight_calendar/blob/979848b9624098812765ba1939098ada1f68eff2/custom_components/skylight/__init__.py)
expose `skylight.create_recipe` and `skylight.plan_meal`, but discard the returned
record IDs and do not return action responses. They also lack public actions for
arbitrary meal-range reads, category/cookbook listing, recipe updates/deletes, and
meal updates/deletes. Reliable automation will need those public response actions
or an explicit supported client interface; avoid depending on private `hass.data`.

Its [meal sensors](https://github.com/MegaTheLEGEND/skylight_calendar/blob/979848b9624098812765ba1939098ada1f68eff2/custom_components/skylight/sensor.py)
expose today's enriched sittings and recurring `instances`. The
[coordinator](https://github.com/MegaTheLEGEND/skylight_calendar/blob/979848b9624098812765ba1939098ada1f68eff2/custom_components/skylight/coordinator.py)
fetches a limited forward window and can retain a partial result after a failed
meal fetch. Neither an empty today sensor nor absence from that window establishes
that a meal or recipe was deleted. Calendar event entities are separate from the
Skylight meal planner; creating a calendar event does not create a planned meal.

## Remaining controller work

A future bridge needs a durable mapping journal keyed by Recipe Keeper account,
Skylight frame, both record IDs, and (for recurring sittings) occurrence date. Save
last agreed revisions/content and outbound operation IDs before sending changes.
Hold affected records whenever `unresolved_operations` is nonempty, or use the
existing download-only reconciliation action to resolve those operations first.
A fresh cache alone does not prove an uncertain edit never reached the cloud.

Begin with a dry-run comparison and an explicit authoritative direction. Detect
changes on both sides against the last agreed baseline and ask for review on
conflicts. Do not infer deletions from absence, stale/failed/partial reads, date
window movement, or an empty cookbook response. Only propagate an explicit
confirmed tombstone through an existing owned-ID mapping, with deletion separately
enabled. Remote retention may prune tombstones, so absence cannot replace a journal.

Recurring meals should expand only within a configured planning horizon, preserving
series and occurrence IDs in that journal. Recipe Keeper has no equivalent exposed
recurrence rule, so reverse propagation needs a defined occurrence policy. Empty
Skylight slots and free-text Recipe Keeper meals also need an explicit policy.

No Skylight credentials, live reads/writes, deployments, or actual recipe/meal
changes were used for this preparation. The new snapshot tests use synthetic data.
