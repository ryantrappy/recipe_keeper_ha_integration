# Home Assistant connection plan

This project implements the database bridge. It does not yet install a custom
Home Assistant integration, modify your Home Assistant instance, or write to a
shopping list. These can now be built against a small local API.

## First connection

1. Deploy the bridge to the MacBook and establish connectivity from the actual
   Home Assistant runtime (see [deployment](deployment.md)).
2. Store the bridge token in Home Assistant's secrets or integration credentials.
   Send it in the `Authorization` header, never in a URL.
3. Confirm `/v1/status`, search `/v1/recipes?q=...`, and retrieve a recipe by ID.
4. Retrieve `/v1/meal-plan` for the desired calendar week. Check `match` on each
   meal. Free-text meals and duplicate titles require an explicit choice.

## Shopping-list consumer

The next component should fetch `/v1/shopping-preview` for a chosen date range,
surface review issues, and write selected lines to your chosen shopping-list
provider. It should persist the keys it successfully imports and handle partial
failures so polling does not repeatedly append the same lines. A crash between
the list write and key persistence still needs reconciliation; stable keys alone
do not make an external list API idempotent.

Keep an explicit relationship between a list item and its contributing meals
if you later want quantity aggregation or edits to propagate. Decide whether
removing a planned meal should remove anything already purchased or checked off.
Start with an explicit "add this week's ingredients" action, then automate once
that behavior is verified against the shopping list you use.

Recipe Keeper ingredients are free text. Quantity conversion, scaling non-unit
servings, pantry exclusion, section headings, and combining similar ingredients
need additional rules. This API preserves text and reports known uncertainty;
it does not silently reinterpret recipe quantities.

## Availability and freshness

Treat 503, connection failure, or an asleep Mac as unavailable data. An empty
successful meal plan is distinct from a failed read. Keep previous list entries
when the bridge is unavailable. The bridge observes local sync results only;
the status timestamp is a file modification time, not a cloud synchronization
guarantee.
