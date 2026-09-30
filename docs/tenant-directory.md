# Setting up a tenant in a client's dbt repo

A tenant is one directory. It lives in the client's dbt repository next to the project it describes (ADR 0001), so a change to a metric, its trap and its golden question goes in one pull request the client reviews. Understory reads the directory. It never owns it. The four tenants under `tenants/` in this repo are fixtures.

## Layout

```
<client dbt repo>/
  dbt_project.yml
  models/
  target/semantic_manifest.json   written by `dbt parse`; Understory reads it here
  understory/
    tenant.yml                    warehouse, semantic layer, limits, sql scope, log, auth, checks
    context.md                    the get_context document
    traps.yml                     the traps registry (design section 5)
    golden/
      questions.yml               the trap set
      realistic.yml               the realistic set
```

The directory doesn't have to be called `understory/`. That name is the convention.

## Paths in tenant.yml

A relative path to something the tenant reads resolves against the tenant directory. This applies to `warehouse.path` (DuckDB), `warehouse.credentials_file` (BigQuery), `semantic_layer.dbt_project_dir`, `profiles_dir`, `cache_dir` and `manifest`. A tenant inside the dbt repo says `dbt_project_dir: ..` and works in any checkout or mount. Understory leaves absolute paths, URLs and `md:` MotherDuck names alone.

Two kinds of value do not resolve this way:

- Log prefixes (`log.events_prefix`, `text_prefix`, `gaps_prefix`). These are outputs, and the log should never end up in the client's repo. Point them at a `gs://` bucket, or at `${UNDERSTORY_LOG_DIR}`, which the container sets to `/var/understory-log`.
- `semantic_layer.env`. It goes to the `mf` subprocess as is, and `mf` runs with the dbt project as its working directory.

`${VAR}` and `${VAR:-default}` expand everywhere, so one file can serve a laptop and the container.

## The manifest

Understory takes the first of these that exists:

1. `manifest:` in tenant.yml, a path to the file.
2. `semantic_manifest.json` in the tenant directory.
3. `<dbt_project_dir>/target/semantic_manifest.json`, which `dbt parse` leaves behind.

In `metricflow_local` mode the third needs no copy step, as long as `dbt parse` runs before the server starts. In `dbt_cloud` mode there is no local dbt project, so the deploy has to put a manifest where 1 or 2 can find it.

## A minimal tenant.yml

```yaml
name: northern_nights
display_name: "Northern Nights"

warehouse:
  type: bigquery
  project: "${BQ_PROJECT}"
  credentials_file: "${UNDERSTORY_BQ_KEY:-}"    # empty means application default credentials

semantic_layer:
  type: metricflow_local
  dbt_project_dir: ..
  cache_dir: "${UNDERSTORY_CACHE_DIR:-../target/understory-cache}"

sql:
  schemas: [marts]

log:
  events_prefix: "${UNDERSTORY_LOG_DIR:-.understory-log}/northern_nights/events"
  text_prefix: "${UNDERSTORY_LOG_DIR:-.understory-log}/northern_nights/text"
  gaps_prefix: "${UNDERSTORY_LOG_DIR:-.understory-log}/northern_nights/gaps"

auth:
  mode: static
  tokens:
    pilot: "${UNDERSTORY_TOKEN_PILOT}"
```

The full schema, with defaults, is `TenantConfig` in `src/understory/tenant.py`.

## Running against it

Every command takes `--tenant` as a directory, a `tenant.yml`, or the name of a fixture under `tenants/`. When the flag is missing, the command reads `UNDERSTORY_TENANT`.

```bash
export UNDERSTORY_TENANT=~/code/client-dbt/understory
uv run understory traps check            # CI step in the client repo
uv run understory check                  # manifest, traps, warehouse, freshness
uv run understory draft-golden --append  # writes into the client's golden/questions.yml
uv run understory eval --deterministic
```

`understory promote-gaps` appends the backlog's gaps to `golden/realistic.yml` as open items. The file goes through the client's normal review, which is the point where someone reads the question text before it is committed.

The harness writes eval reports to `<tenant>/.evals/`, and the default compile cache sits beside the manifest. Add both to the client's `.gitignore`:

```
understory/.evals/
understory/.cache/
```

## In the container

One container per tenant. Mount the client repo, or at least the tenant directory and the dbt project, and point `UNDERSTORY_TENANT` at the tenant directory. The image sets it to `/tenant`.

```bash
docker run -p 8000:8000 \
  -v ~/code/client-dbt:/repo:ro \
  -e UNDERSTORY_TENANT=/repo/understory \
  -e UNDERSTORY_CACHE_DIR=/tmp/understory-cache \
  -v understory-log:/var/understory-log \
  understory
```

A read-only mount is fine. When the compile cache can't be written, Understory logs a warning and compiles every query instead of failing. Setting `UNDERSTORY_CACHE_DIR` to a writable path keeps the cache.
