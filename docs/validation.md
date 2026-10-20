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
