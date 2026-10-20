# Choose which ingredients to buy

Ingredient previews support selecting a subset of lines. A pantry staple is a
default suggestion to skip an ingredient, not a deletion from a recipe or a
permanent ban on purchasing it.

For example, the planned HACS review step should look like this:

- [ ] 2 tablespoons olive oil — always stocked
- [x] 1 can white beans
- [x] 1 lemon

Only selected ingredients belong in the eventual list import. The user can
select olive oil when running low or unselect beans already in the cupboard.
The full recipe and its ingredient quantities remain unchanged.

## Current API support

The selection logic works with either backend. Set pantry staples in `.env`:

```dotenv
RECIPE_ASSISTANT_PANTRY_STAPLES='["olive oil"]'
```

Restart the server after editing this setting. No other staples are assumed.
The user's local configuration currently includes olive oil, as requested.

`GET /v1/shopping-preview?start=YYYY-MM-DD&end=YYYY-MM-DD` returns:

- `items`: every eligible ingredient line, with `selected` and `pantry_match`.
- `selected_items`: the subset selected by default, excluding matched staples.
- `ready` and `issues`: existing recipe/scale review status, preserved unchanged.

After choosing lines, submit their stable keys to the same path with POST:

```json
{
  "start": "2026-10-19",
  "end": "2026-10-25",
  "selected_keys": ["<ingredient key from the preview>"]
}
```

The returned `selected_items` contains only those choices. An explicit selection
can include a pantry staple without changing the saved preference. An empty
list selects nothing; omitted/null keys apply the pantry defaults. This POST
only computes a selection; it does not write to a shopping list or cloud data.
If a selected key disappeared because ingredients or planned meals changed, the
API returns 409 and asks for a fresh preview instead of silently importing a
different set. Invalid date ranges return 422.

Pantry matching ignores case and handles common quantities, measures, and
descriptions such as `extra-virgin olive oil`. It deliberately leaves uncertain
or distinct foods selected: `olive oil mayonnaise`, `olive oil cooking spray`,
and `olive oil or butter` are not automatically treated as plain olive oil.
Use full ingredient names in pantry rules. The matcher is conservative, not a
complete natural-language ingredient parser; all lines stay available for review.

This selects whole ingredient lines. Splitting a quantity, such as buying only
half the required flour, needs a separate quantity editing feature.

## HACS implementation requirements

The native custom integration should persist pantry rules per account through
HA storage/options rather than depend on the standalone API's `.env`. It should
reuse the same selection behavior in its client/domain library.

The review interface and list-import action still need building. They must:

1. Show all eligible ingredients and the reason a staple starts unselected.
2. Allow per-shop choices, including overrides of pantry defaults.
3. Submit only explicit selected keys to the import action.
4. Retain stale-data and unresolved-recipe warnings.
5. Persist successful imports to avoid duplicates, with reconciliation for
   partial failures and changed meals.

A pantry preference affects future previews; it should not delete an item
already on the shopping list or mark it purchased. The eventual list writer
must consume `selected_items`, never blindly copy every entry from `items`.
