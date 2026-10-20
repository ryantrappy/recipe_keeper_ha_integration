# Security and private data

Recipe Keeper access requires credentials for your own account. This repository
contains source code and synthetic fixtures, not shared credentials or a hosted
service. Publishing the repository does not expose an installed Home Assistant
instance or the Recipe Keeper account.

## Runtime boundaries

- The integration uses Home Assistant authentication and its shared HTTPS session.
  Cloud URLs are fixed, TLS verification remains enabled, and redirects are rejected.
- Cloud recipe/meal editing defaults off. Editing actions and shopping imports
  require an administrator context; trusted HA automations can use a system context.
  Read actions are available to authenticated HA users and include private recipes.
- The edit flag is a client policy, not a separate read-only cloud account scope.
  The stored Recipe Keeper credentials remain capable of signing in to that account.
- The optional API binds to loopback by default and requires a random bearer token
  for all data endpoints, including synchronization exports. Keep it behind an SSH
  tunnel, VPN, or authenticated HTTPS reverse proxy for remote access. Do not expose
  its plain HTTP listener to the Internet or transmit the bearer token over public HTTP.
- Recipes and cloud errors are not rendered as HTML. The panel uses text insertion
  for recipe fields; credentials/tokens are excluded from diagnostic responses.
- The integration never writes the Recipe Keeper Mac application database. Optional
  edits use the cloud protocol with durable intent, explicit IDs, revision checks,
  read-back verification, and no automatic replay of unknown outcomes.

## Files to protect

HA integration configuration and HA backups contain the account credentials. Its
private cache/journal contains full recipes and photos needed to preserve existing
fields. Shopping receipts contain ingredient source hashes and import state.

For the standalone API, `.env`, `local-data/`, databases and WAL/lock files, backups,
logs, and build outputs are Git-ignored. Local credentials/cache files use private
permissions. These files are not encrypted by this project; restrict access to the
OS account and protect backups. Do not upload them in issues or screenshots.

`get_sync_snapshot`, recipe actions, exported JSON, and HA automation traces can
contain the entire cookbook and meal plan. They are intentionally accessible to
authorized callers; do not post those responses publicly. Snapshot account hashes
are namespaces, not anonymization or access tokens.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository when available.
Do not post passwords, bearer tokens, private recipe exports, unredacted HA logs,
or backups in a public issue. Include the affected version and a minimal synthetic
reproduction. Ordinary bugs can be filed in Issues after removing private data.

## Review record

See [the security audit record](docs/security-audit.md) for the scope and results of
the 2026-10-20 checks. Automated scans and code review reduce risk; they cannot
prove the absence of every vulnerability or guarantee the private cloud protocol.
