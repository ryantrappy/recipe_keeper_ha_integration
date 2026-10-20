# Public repository security review — 2026-10-20

Scope: the five existing reachable commits through `b93f11f`, current tracked and
shipping source/docs, installation packaging, and the new sync snapshot interface.
Reports were saved locally under Git-ignored `local-data/security-audit`; no actual
credentials or private-data samples are reproduced here.

## Results

- Exact matching for the configured Recipe Keeper email/password and local API
  token checked **114 reachable Git blobs** across all refs: **zero matches**.
- Historical paths contained **no** `.env` credentials file, `local-data/`, virtual
  environment, database, cache, or build output.
- **Gitleaks 8.30.1** found **zero findings** in the complete reachable Git history
  and a separate copy of the current shipping files. The official release binary's
  SHA-256 checksum was checked before use. Private local files were excluded from
  the shipping-file scan, not uploaded to a service.
- **pip-audit 2.10.1** found **zero known vulnerabilities** in **23 locked standalone
  runtime packages**, and separately in the **14 installed HA client dependency
  packages** (including HA's installed pydantic version). This checks known advisory
  data for those versions, not every package in HA OS or the upstream Skylight code.
- Bandit scanned source and HA adapters, with the generated duplicate client excluded.
  Its seven findings were reviewed: placeholder-token rejection, empty-token clearing,
  a boolean token-summary flag, SQL assembled exclusively from fixed table/column
  allowlists, and an assertion reached only after a non-null check. No untrusted
  SQL identifier or embedded usable credential was found. These are explained
  scanner matches, not seven unresolved vulnerabilities.

## Code and documentation checks

API data reads/writes require bearer authentication; the new snapshot does too.
HA cloud writes/imports retain admin enforcement and default-off editing. Cloud
transport keeps TLS validation and rejects redirects. The panel inserts recipe text
with textContent, not HTML interpolation. Diagnostic data omits credentials and full
recipe/meal bodies. Exports include only declared fields, not raw photos, unknown
fields, credentials, or private edit baselines/payloads.

Filesystems and packages retain the existing ownership/permission checks. Release
ZIPs include only runtime file types from the component folder; client source
provenance is checked against canonical files. Build/test reports and private caches
remain ignored. README/setup examples were changed to generic checkout paths so
public instructions do not depend on a personal user directory.

Workflow permissions are limited to contents:read and HACS PR comments are disabled.
All six GitHub Action dependencies were pinned to verified upstream commit SHAs.
Checkout credentials are not persisted into the build/test checkout. Third-party
packages and container images still depend on their upstream publishers; pinning
an action does not make those dependencies immune to future compromise.

## Remaining boundaries

This review found no exposed usable credentials or confirmed exploitable issue in
the reviewed project. It is not a guarantee that code has no security risk. Keep
Home Assistant and HACS updated, protect configuration/backups and recipe exports,
and use encrypted transport for any remote API access. The cloud editing flag is
client-enforced, and simultaneous changes from another device can still overwrite
changes because the tested service has no established compare-and-swap guarantee.

The baseline integration previously passed iterative independent review. This
update was checked directly with regression tests, source review, and the scans
above; it does not claim a separate completed independent review of this increment. No actual recipes, planned meals, shopping lists, or production HA
configuration were modified during this review.
