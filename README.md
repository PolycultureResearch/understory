# Understory

The natural-language interface for analytics, by [Polyculture Research](https://github.com/PolycultureResearch).

Understory is an MCP server that exposes a client's dbt semantic layer to the chatbot their employees already use (ChatGPT Enterprise, Claude, or a Slack bot). It asks when a question is ambiguous, says "we don't know" when the data can't answer, falls back to labeled SQL when the semantic layer can't, and shows where every number came from. Every question, clarification, refusal, and answer is logged so the semantic layer gets better over time.

The billable work at each client is the dbt model and semantic layer. Understory is the reusable part that turns that work into something people can use through chat within days.

## Design

- [MVP design](docs/understory-mvp-design.md). Tools, the traps registry, semantic layer and warehouse adapters, identity and logging, the eval harness, and the build sequence.
- [Roadmap](docs/understory-roadmap.md). What comes after the MVP and why it waits, including the Breakdown-based explanation engine for "why did X change" questions.
- [Architecture draft 0.1](agentic-analytics-architecture.md). The original design. The MVP design supersedes its sections 3 through 8.

## Stack

Python 3.13, the official MCP SDK, open-source MetricFlow, DuckDB and BigQuery, Parquet, dbt, sqlglot, Pydantic AI over OpenRouter. Development and tests run against [fake_companies](https://github.com/PolycultureResearch/fake_companies), never client data.

## Status

Design stage. No code yet.

## Related

- [Breakdown](https://github.com/PolycultureResearch/breakdown). Bayesian metric trees and root cause analysis. Understory will call it for explanation questions.
- [fake_companies](https://github.com/PolycultureResearch/fake_companies). Synthetic company data with dbt projects and labeled anomalies for four verticals.
