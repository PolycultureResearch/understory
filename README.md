# Understory

The natural-language interface for analytics, by [Polyculture Research](https://github.com/PolycultureResearch).

Understory is an MCP server that exposes a client's dbt semantic layer to the chatbot their employees already use (ChatGPT Enterprise, Claude, or a Slack bot). It asks when a question is ambiguous, says "we don't know" when the data can't answer, falls back to labeled SQL when the semantic layer can't, and shows where every number came from. Every question, clarification, refusal, and answer is logged so the semantic layer gets better over time.

The billable work at each client is the dbt model and semantic layer. Understory is the reusable part that turns that work into something people can use through chat within days.

## Design

- [MVP design](docs/understory-mvp-design.md). Tools, the traps registry, semantic layer and warehouse adapters, identity and logging, the eval harness, and the build sequence.
- [Roadmap](docs/understory-roadmap.md). What comes after the MVP and why it waits, including the Breakdown-based explanation engine for "why did X change" questions.
- [Architecture draft 0.1](agentic-analytics-architecture.md). The original design. The MVP design supersedes its sections 3 through 8.

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
uv run pytest                                            # 170 tests; DuckDB and mf tests skip if data is absent
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
  telemetry/   write-only Parquet log in two families, HMAC user hashing
  harness/     Pydantic AI agent over OpenRouter, golden sets, eval runner
dbt_understory/  dbt package modeling the log: fct_questions, fct_sessions, mart_eval_daily, ...
tenants/         one directory per client; four fake_companies tenants committed
```

A tenant is a directory: `tenant.yml`, `context.md`, `traps.yml`, `semantic_manifest.json`, and `golden/questions.yml`.

## Stack

Python 3.13, the official MCP SDK, open-source MetricFlow, DuckDB and BigQuery, Parquet, dbt, sqlglot, Pydantic AI over OpenRouter.

## Status

MVP on the `mvp-scaffold` branch. The server, all seven tools, the telemetry package, and the harness work end to end against the four fake tenants. The deterministic eval passes every golden item on every tenant, and the live path is verified through OpenRouter. Not yet done: a deployment against a real client warehouse, and connector auth against an identity provider.

```bash
export OPENROUTER_API_KEY=...
uv run understory ask --tenant tenants/alpenglow "How were sales in the US in March 2025?"
uv run understory eval --tenant tenants/alpenglow --deterministic     # no LLM, runs in CI
uv run understory eval --tenant tenants/alpenglow                     # live, writes tenants/alpenglow/.evals/
```

## Related

- [Breakdown](https://github.com/PolycultureResearch/breakdown). Bayesian metric trees and root cause analysis. Understory will call it for explanation questions.
- [fake_companies](https://github.com/PolycultureResearch/fake_companies). Synthetic company data with dbt projects and labeled anomalies for four verticals.
