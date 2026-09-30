# Understory

The natural-language interface for analytics, by [Polyculture Research](https://github.com/PolycultureResearch).

Understory is an MCP server that exposes a client's dbt semantic layer to the chatbot their employees already use (ChatGPT Enterprise, Claude, or a Slack bot). It prefers a declared reading and says so when a question is ambiguous, asks only where the client chose to, says "we don't know" when the data can't answer, falls back to labeled SQL when the semantic layer can't, and shows where every number came from. Every question it could not answer is recorded as a gap, with the SQL that answered instead, so the data team's backlog writes itself.

The billable work at each client is the dbt model and semantic layer. Understory is the reusable part that turns that work into something people can use through chat within days.

## Design

- [Design](docs/understory-mvp-design.md), draft 0.3. Tools, the traps registry, gaps, semantic layer and warehouse adapters, identity and logging, the two eval sets, and the build sequence.
- [Glossary](CONTEXT.md). The vocabulary the design and code use. [Decisions](docs/adr/) records the ones that were hard to reverse. [Knowledge](knowledge/) holds dated measurements.
- [Setting up a tenant](docs/tenant-directory.md). How a client's tenant directory sits in their dbt repo, how its paths resolve, and how the container mounts it.
- [Roadmap](docs/understory-roadmap.md). What comes after the MVP and why it waits, including the Breakdown-based explanation engine for "why did X change" questions.
- [Architecture draft 0.1](agentic-analytics-architecture.md). The original design. The design supersedes its sections 3 through 8.

## Quickstart

Understory develops against [fake_companies](https://github.com/PolycultureResearch/fake_companies), four synthetic companies with dbt projects and MetricFlow metrics on DuckDB. Clone it as a sibling directory (or set `FAKE_COMPANIES_DIR`), generate a company, and build its dbt project:

```bash
cd ../fake_companies && uv sync --extra dbt
uv run fake-companies generate --config configs/alpenglow_retail_dtc.yaml --out out/alpenglow.duckdb
DBT_PROFILES_DIR=dbt/retail_dtc FAKE_DB=$PWD/out/alpenglow.duckdb uv run dbt build --project-dir dbt/retail_dtc
```

Then, in this repo:

```bash
uv sync --all-extras
uv run understory check --tenant tenants/alpenglow      # manifest, traps, warehouse, freshness
uv run understory serve --tenant tenants/alpenglow      # MCP over streamable HTTP at :8000/mcp
uv run pytest                                            # 280 tests; DuckDB and mf tests skip if data is absent
```

Point any MCP client at `http://127.0.0.1:8000/mcp`. For a chatbot on the internet, run the container and put it behind HTTPS with `auth.mode: static` in `tenant.yml`.

## Layout

```
src/understory/
  server/      MCP tools (mcp.py), tool logic (service.py), sessions, windows, auth, serve CLI
  catalog/     semantic_manifest.json reader, get_context assembly, describe
  traps/       traps registry schema, matcher, CI check, `understory traps check`
  semantic/    SemanticLayer: metricflow_local (mf query --explain + cache), dbt_cloud
  warehouse/   Warehouse: duckdb, bigquery
  guard/       sqlglot read-only SQL guard for run_sql
  telemetry/   write-only Parquet log in three families (events, text, gaps), HMAC user hashing
  harness/     Pydantic AI agent over OpenRouter, trap and realistic sets, eval runner, golden and realistic drafting
dbt_understory/  dbt package modeling the log: fct_questions, fct_sessions, mart_eval_daily, ...
tenants/         one directory per client; four fake_companies tenants committed
```

A tenant is a directory holding `tenant.yml`, `context.md`, `traps.yml` and `golden/`. The golden directory has two sets. `questions.yml` is the trap set and guards correctness. `realistic.yml` is the realistic set and guards adoption. The semantic manifest can sit in the tenant directory or come from the dbt project's `target/`. The fake tenants here are fixtures. A client's tenant lives in their own dbt repository, usually as `understory/` beside `dbt_project.yml`, and [docs/tenant-directory.md](docs/tenant-directory.md) covers the setup.

Every command takes `--tenant` as a directory, a `tenant.yml`, or a fixture name (`--tenant alpenglow`), and falls back to `UNDERSTORY_TENANT`. Relative paths in `tenant.yml` resolve against the tenant directory, so `dbt_project_dir: ..` works wherever the client repo is checked out or mounted.

## Stack

Python 3.13, the official MCP SDK, open-source MetricFlow, DuckDB and BigQuery, Parquet, dbt, sqlglot, Pydantic AI over OpenRouter.

## Status

Steps 1 through 7 of the build sequence (design section 14) are built. That covers harness efficiency, the policy flip, gaps, the realistic set, golden authoring, the external tenant mount and gap promotion. The server, all seven tools, the telemetry package and the harness run end to end against the four fake tenants, and against a tenant mounted from outside this repo. Both golden sets pass deterministically on every tenant. The live path works through OpenRouter in both modes and on both model tiers. On Sonnet with the enforced closing check, alpenglow scores 21/23 on the realistic set and 10/11 on the trap set (`knowledge/harness-last-step-2026-09-22.md`).

Next are the first friendly users on a fake tenant, through Claude.ai over a tunnel. Nothing has run against a real client warehouse yet, and connector auth against an identity provider isn't built.

```bash
export OPENROUTER_API_KEY=...
uv run understory ask --tenant tenants/alpenglow "How were sales in the US in March 2025?"
uv run understory eval --tenant tenants/alpenglow --deterministic     # trap set, no LLM, runs in CI
uv run understory eval --tenant tenants/alpenglow                     # live, writes tenants/alpenglow/.evals/
uv run understory eval --tenant tenants/alpenglow --set realistic --deterministic
uv run understory eval --tenant tenants/alpenglow --set realistic --tiers        # default and cheap model, compared
uv run understory eval --tenant tenants/alpenglow --set realistic --tiers --byo  # connector surfaces only
```

The realistic set's report leads with first-turn answer rate and over-refusal rate, broken down by item kind (`event`, `quiet`, `over_refusal`, `fault`). `--byo` runs the agent on the connector instructions and tool descriptions alone, which is what a client's chatbot sees. The harness runs the closing `log_answer` check itself when the model forgets (`--no-enforce-check` turns that off) and its system prompt is a variable (`--prompt harness|lean`). To measure a prompt change, pick the items that carry the variance and repeat them:

```bash
uv run understory eval --tenant tenants/alpenglow --set realistic --repeat 5 \
  --id sales_by_month_2025_q1 --id refunds_why_2024_10 --prompt lean
```

A client's trap set starts drafted too. `draft-golden` writes one canonical item per metric and per metric-by-dimension from the catalog, and one item per trap from the registry; `verify` marks the snapshots a person has checked against a known report; the `golden-interview` skill turns an hour with the data owner into the items neither can know.

```bash
uv run understory draft-golden --tenant tenants/alpenglow --append         # add what golden/questions.yml lacks
uv run understory fill --tenant tenants/alpenglow --set trap
uv run understory verify --tenant tenants/alpenglow --list                 # the numbers still to check
uv run understory verify --tenant tenants/alpenglow net_revenue_2026_05    # a person checked it
```

Gaps come back as golden items. `promote-gaps` reads `mart_semantic_backlog` and appends one item per gap to the realistic set, with the question, who hit it and how often, what was missing, and the SQL that answered instead. The item stays open, listed by the evals but not scored, until someone builds the metric and adds a spec. Run it weekly; it only adds gaps it has not seen.

```bash
uv run understory promote-gaps --tenant tenants/alpenglow --relation analytics_understory.mart_semantic_backlog --dry-run
uv run understory promote-gaps --tenant tenants/alpenglow --db /tmp/understory_dev.duckdb --relation main.mart_semantic_backlog
```

The realistic set is drafted, not written from scratch. `draft-realistic` reads the seeded ground truth in the warehouse and writes template questions with correct specs; someone rewrites the wording and adds over-refusal items; `fill` runs every spec once and records the numbers as a snapshot with `verified: false`.

```bash
uv run understory draft-realistic --tenant tenants/alpenglow          # writes golden/realistic.yml from meta.ground_truth
uv run understory fill --tenant tenants/alpenglow                     # snapshots numbers into the file, in place
```

## Related

- [Breakdown](https://github.com/PolycultureResearch/breakdown). Bayesian metric trees and root cause analysis. Understory will call it for explanation questions.
- [fake_companies](https://github.com/PolycultureResearch/fake_companies). Synthetic company data with dbt projects and labeled anomalies for four verticals.
