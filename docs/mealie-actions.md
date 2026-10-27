# Meal planning and automation actions

Version 0.3.0 adds equivalents for the [Mealie integration's documented
actions](https://www.home-assistant.io/integrations/mealie/#list-of-actions).
Use the `recipe_keeper` domain. Existing actions and the combined meal calendar
retain their names and IDs, so existing automations continue to work.

## Actions

All actions select an account with `config_entry_id` and return responses. Use
`response_variable` in automations. Obtain the account ID with
`recipe_keeper.get_accounts`.

| Mealie action | Recipe Keeper action | Inputs / response |
| --- | --- | --- |
| `get_recipes` | `get_recipes` | Optional `search_terms`, `result_limit` (1–100, default 10), `offset`; returns `recipes.items`, `recipes.total`, and pagination |
| `get_recipe` | `get_recipe` | `recipe_id`; returns `recipe` with full ingredients, directions, notes and categories |
| `get_mealplan` | `get_mealplan` | Optional inclusive `start_date`, `end_date`, each defaulting to today in HA's timezone; returns `mealplan` with IDs, revisions, recipe matches and contents |
| `set_mealplan` | `set_mealplan` | `operation_id`, `date`, `entry_type`, and either `recipe_id` or `note_title` with optional `note_text` |
| `set_random_mealplan` | `set_random_mealplan` | `operation_id`, `date`, `entry_type`; optional `search_terms` and `favourite_only` |
| `update_mealplan` | `update_mealplan` | Same selection fields as `set_mealplan`, plus `mealplan_id` and its current `revision` |
| `delete_mealplan` | `delete_mealplan` | `operation_id`, `mealplan_id`, current `revision` |
| `import_recipe` | `import_recipe` | `operation_id`, public HTTPS `url`; optional `include_tags` and zero-based `recipe_index` |
| `get_shopping_list_items` | `get_shopping_list_items` | Optional `todo_entity` (otherwise configured default) and `status` list (default `[needs_action]`); returns `items` and the HA list ID |

The original `search_recipes` and `get_meal_plan` actions remain available with
their original fields and response shapes. The new names use the mappings above;
this is functional compatibility, not a promise of identical Mealie JSON.
Recipe Keeper uses record IDs; Mealie recipe slugs are not available.

Meal types are `breakfast`, `lunch`, `dinner`, `side`, `dessert`, `drink`, and `snack`.
Setting a meal **adds one meal record**; it does not replace every meal in that
date/type slot. Use an explicit update or delete for an existing record. Note
planning supports meals such as “Eat out” without creating a recipe.

Recipe Keeper links meals to recipes by title. Planning by ID rejects empty or
duplicate titles; random selection excludes them. A random choice is saved before
upload, and retrying the same operation uses that saved choice. Search matches
recipe titles and ingredients. There is no unverified recipe-ID field written to
the cloud.

## Safe writes and retries

All five new writing actions require an administrator (or HA automation system
context) and **Allow editing Recipe Keeper** enabled in Configure. Reads and HA
shopping-list imports work with Recipe Keeper editing disabled.

Use a new UUID `operation_id` for each intended change. Reuse that UUID with the
same inputs when checking a retry; never generate a new one for a possibly
completed request. Responses include `operation_id`, `entity_id`, and `state`;
meal-planning actions also return `mealplan_id`, and recipe selections return
`recipe_id`. Confirmed imports include `recipe`, and confirmed plans include the
cached `mealplan` record and revision. Only `state: confirmed` verifies the cloud result. For `uncertain`,
use `reconcile_edit` or the panel's **Check saved edit**. Confirmed replay does not
upload again. Imported contents and random choices survive restarts in the private
cache, alongside the existing edit journal.

For update/delete, use the exact `revision` from `get_mealplan` (or
`get_editing_view`). A stale revision stops the upload. Recipe Keeper still has no
verified server-side compare-and-swap contract, so simultaneous edits on another
device can race with an upload. Other recipe fields, photos, and meal metadata
are preserved by the shared editor.

Example read for a dashboard or spoken dinner announcement:

```yaml
- action: recipe_keeper.get_mealplan
  data:
    config_entry_id: YOUR_ENTRY_ID
    start_date: "{{ now().date().isoformat() }}"
    end_date: "{{ now().date().isoformat() }}"
  response_variable: planned
- variables:
    dinner_titles: >-
      {{ planned.mealplan
         | selectattr('meal_type', 'equalto', 'Dinner')
         | map(attribute='title') | list }}
```

Example write from Developer Tools → Actions. Replace the UUID for each new
intended meal; preserve it for a retry of that same meal:

```yaml
action: recipe_keeper.set_mealplan
data:
  config_entry_id: YOUR_ENTRY_ID
  operation_id: "36cb22d9-3712-477d-bfea-15eb2a94bcec"
  date: "2026-10-28"
  entry_type: dinner
  note_title: "Eat out"
  note_text: "Dinner with friends"
response_variable: saved_meal
```

## Calendars

HA creates the existing **Meal plan** calendar plus **Breakfast**, **Lunch**,
**Dinner**, **Side**, **Dessert**, **Drink**, **Snack**, and **Other meals** calendars
for each account. Case and surrounding whitespace in meal labels are ignored;
custom/blank labels go into Other meals. Each meal occurs in the combined calendar
and exactly one type calendar. Select either the combined calendar or individual
calendars on a dashboard to avoid displaying meals twice.

Events are all-day events in HA's timezone, with an exclusive next-day end and
stable cloud record UIDs. They work with `calendar.get_events`, calendar cards and
calendar automation triggers, including start offsets for dinner reminders. The
cache refreshes every five minutes; explicit writes also refresh it. Generic
calendar editing is not advertised; use the guarded meal actions for writes.

## Shopping lists and statistics

Shopping lists remain **Home Assistant `todo` entities**, as configured for the
existing selective ingredient importer. `get_shopping_list_items` calls HA's
`todo.get_items` with the caller's context and returns the requested list's items.
Use native `todo.add_item`, `todo.update_item`, `todo.remove_item` and
`todo.remove_completed_items` actions for list management. This integration does
not mirror Recipe Keeper's own cloud shopping lists or create duplicate HA lists.
Pantry staples and per-ingredient checkboxes continue to control recipe imports.

Sensors include recipe, meal and **category counts**, last successful sync, data
freshness and editing status. Recipe Keeper's current sync contract does not
provide equivalents for Mealie's user/tool/tag statistics; no artificial counts
are exposed. Website keywords can optionally be imported as Recipe Keeper
categories.

## URL import limits

Import reads schema.org **Recipe JSON-LD** embedded in public HTML. It supports
`@graph`, Recipe type lists, ingredient lists, plain instructions and nested
HowToSection/HowToStep instructions. It keeps the source URL, serving text,
categories/cuisine and description. `include_tags: true` also maps website
keywords to categories. Preparation/cooking/total duration strings are retained
in notes because the native raw time units have not been verified.

Multiple distinct recipes require `recipe_index` instead of silently selecting
one. Missing names, ingredients or instructions fail without a cloud upload.
JavaScript-only pages, sign-in/paywall content, microdata-only pages, photos and
nutrition imports are not supported. Public HTML that ignores `Accept-Encoding:
identity` and insists on compressed responses is rejected. Import does not run
scripts or fetch JSON-LD contexts, images or linked resources.

The fetcher uses a separate credential-free session, verified HTTPS on port 443,
validated public DNS answers, and checks each redirect. It blocks private/local
IP destinations, alternative numeric IP forms, environment proxies and cookies.
HTML is limited to 2 MiB, at most five redirects, and 30 seconds overall; parsing
has nesting/node limits. No Recipe Keeper credentials are sent to recipe sites.
