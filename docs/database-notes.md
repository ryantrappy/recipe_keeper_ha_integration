# Database observations

Inspected locally on 2026-10-19 using Recipe Keeper for macOS 3.46.1 (build 144).
No account preferences, cookies, passwords, or cloud tokens are needed by the
bridge. No private recipe content or database copy is stored in this repository.

The actual database is in the app's `Data/Documents/RecipeKeeper.db`, not its
mostly empty shared Group Container. It is a standard readable SQLite database.
The observed database uses DELETE journaling; the reader also supports WAL.

## Relevant tables

| Table | Relevant columns |
| --- | --- |
| `Recipes` | `Id`, `Title`, `IsDeleted`, `ServingSize`, `Ingredients`, `Directions`, `Notes`, `Source`, preparation/cooking times |
| `Meals` | `Id`, `MealDate`, `Title`, `MealTypeName`, `MealTime`, `Position`, `Scale`, `Notes`, `IsDeleted` |
| `RecipeCategories` | `RecipeId`, `Name` |
| `LinkedRecipes` | `RecipeId`, `LinkedRecipeId` |

Meals reference recipes by title rather than an ID. Inspection of the app's
`Meal.get_Recipe` method showed a call to `SearchRecipeTitlesExact`; the bridge
uses a case-folded exact-title comparison and reports ambiguity rather than
emulating selection of the first matching recipe. Unicode case handling can
differ from .NET's culture-sensitive behavior.

`MealDate` is stored as integer .NET ticks (100 ns since 0001-01-01). For example,
`639280512000000000` represents the planner date 2026-10-20. These are calendar
dates in the app; applying a UTC-to-local conversion would risk shifting a day.

`Scale` is text; all observed active meals had `"1"`. Non-unit and missing
scales are deliberately left for review until scaling behavior is implemented
and verified. Ingredient and direction fields are newline-delimited plain text
in the samples inspected. Serving size is free text and can include a yield
such as a number plus a food name.

The original database was observed to have 72 active recipes and 55 active
meals, with one group of duplicate recipe titles and three unmatched meal
titles. These are observations, not fixtures or expected future counts.

## Read guarantees

Each request opens the source with SQLite `mode=ro`, enables `query_only`, and
uses a short read transaction. Multi-query recipe and meal responses therefore
see one SQLite snapshot. The source connection is closed after reading. No
schema migrations, indexes, writebacks, cloud sync calls, or immutable-mode
assumptions are made. Read locks may briefly delay a writer in DELETE mode.

The reader checks required columns and returns an unavailable response when
the schema changes incompatibly. This is an undocumented third-party schema;
additional semantic changes could require updating the reader even if column
names remain the same.

Implementation references:

- [Python SQLite URI connections](https://docs.python.org/3/library/sqlite3.html#how-to-work-with-sqlite-uris)
- [FastAPI security dependencies](https://fastapi.tiangolo.com/tutorial/security/)
