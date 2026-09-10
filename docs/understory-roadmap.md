# Understory roadmap

Polyculture Research | September 10, 2026

The MVP (`understory-mvp-design.md`) answers descriptive questions through a governed semantic layer, asks when a declared trap fires, falls to labeled SQL when the semantic layer cannot answer, and logs everything for the improvement loop. Everything else from draft 0.1 of the architecture is here, in the order it should be built, with the reason it was deferred and what has to be true before it starts.

Phases are sequential by default. Items within a phase are independent unless noted.

## Phase 1: harden the loop

Goal is to make the MVP trustworthy at two or three clients before adding capability.

### 1.1 User pins

"When I say revenue I mean net." A pin is a stored clarification choice per `user_hash` and trap. The next question skips the ask and adds a disclosure instead. Pins are the first thing users ask for after the third clarification.

Deferred because pins are the first persistent per-user state and the write-only log design has no read path. Pins need a small mutable store outside the log, keyed by `user_hash`, with an unpin tool. Do this before clarification fatigue shows up in the abandonment numbers.

### 1.2 MCP elicitation

Where the chatbot supports elicitation, `query_metrics` requests the clarification through the client with a JSON schema, so the choice never passes through the model's paraphrase. Fallback stays as it is in the MVP.

Deferred because support across ChatGPT Enterprise and Claude.ai is uneven and the fallback works. Revisit when either platform ships it for custom connectors.

### 1.3 Verified queries

A per-tenant set of saved specs for questions asked often, matched by phrase and returned with a `verified: true` provenance flag. These are the semantic layer's "known good" answers and they turn the top of `mart_semantic_backlog` into a fast path.

Depends on a few weeks of production logs to know which questions recur.

### 1.4 Freshness and test status in provenance

Read `run_results.json` and `sources.json` from the dbt deploy and attach upstream test failures and source freshness to every result. A failing test upstream of a metric is disclosed with the result. This is the cheap half of the data health checks in draft 0.1 and it does not need the explanation engine.

### 1.5 Optional LLM critic

An LLM pass inside `log_answer`, through OpenRouter, that checks the draft for causal language, speculation presented as fact, and answers to refused questions. Off by default because it costs money per answer and cannot be enforced in a BYO chatbot. On for the harness and for tenants who ask.

### 1.6 Second chatbot integration

Whichever of Claude.ai or ChatGPT Enterprise the first client does not use. The tool contracts do not change. What changes is connector auth, instruction placement, and the eval capture rate, which we report per platform.

## Phase 2: own the conversation where it helps

### 2.1 Slack bot

The harness with a Slack transport. Same agent, same tools, same OpenRouter model. This is the first deployment where the full loop is enforced, since we control the orchestration. It costs LLM spend per question, which the tenant config caps.

Build when a client asks for it or when the BYO capture rate at a client is low enough that evals cannot tell us whether answers are right.

### 2.2 Result artifacts

Results larger than the row cap are written to Parquet under a per-tenant results prefix and returned as a summary plus a download link. Charts through the chatbot's own rendering. No Understory UI.

### 2.3 Row-level identity

Per-user warehouse identity through OAuth passthrough, for clients whose warehouse enforces row-level security and who have accounts for every user. The MVP's single service account stays the default.

Deferred because it needs every user to have a warehouse account and needs the client to maintain the mapping. Build only when a client's security review requires it.

### 2.4 Multi-tenant process

One process serving several tenants, for when the count of small clients makes one-container-per-client expensive. Not before ten tenants.

## Phase 3: why questions

The explanation engine from draft 0.1, section 4.6. This is the part of Understory that is Polyculture's own, and it stays as designed. The MVP builds toward it by keeping metric names and dimension names aligned between the semantic layer and Breakdown's tree YAML.

### 3.1 `explain_change` over Breakdown

Understory proxies Breakdown's existing MCP server rather than reimplementing it. The tool takes a resolved metric, a window, and a baseline. The baseline is a trap (`ask` or `prefer prior_period`) in the registry. The response carries Breakdown's localization with intervals and the residual share, tiered as in draft 0.1.

Depends on a metric tree per tenant, which is billable work in its own right. fake_companies trees exist for all four verticals.

### 3.2 Data health first

The full stage 1 from draft 0.1. Freshness against SLA, upstream test failures, row-count and null-rate anomalies, recent backfills from deploy history. A blocking issue leads the report. Builds on 1.4.

### 3.3 Business event log

`fct_business_events` in the dbt package, with a seed-file workflow first and connectors (releases, price changes, campaigns, pipeline incidents) second. Event matching on temporal, scope, and direction fit, reported as match strength.

The open question from draft 0.1 stands. Who maintains the event log at the client, and what happens to explanation quality when they stop. Start with the seed file and one connector the client already has, usually a deploy log, and measure the residual share trend before building more.

### 3.4 Explanation evals

Golden incident sets from `ground_truth.json` in fake_companies, which labels every injected anomaly with its affected metrics and dates. Top-two hit rate, residual share, event-match precision. Client incident sets come from post-mortems.

### 3.5 Tier checks in the critic

Causal language against quantified tiers, speculation without label, magnitudes in the speculative tier. Extends 1.5.

## Phase 4: platform

### 4.1 OSI export

When MetricFlow reads synonyms and `ai_context` from the Open Semantic Interchange spec, move them out of `meta`. The traps registry stays separate because OSI has no concept of ask-versus-disclose policy.

### 4.2 Alternative compilers

A third `SemanticLayer` implementation if a maintained open-source compiler from `semantic_manifest.json` to SQL appears, or if Breakdown's bridge grows beyond one measure and one dimension. Removes dbt-core from the container.

### 4.3 Other analytical tools

`explain_change` is the first of a family. Forecasts, anomaly detection (Tremor), and what-if simulation from Breakdown each become a tool with the same provenance and tier rules. Understory stays the interface and the tools stay separate services.

### 4.4 Embedding-based synonym suggestion

Runs offline over the `text` prefix, proposes synonyms and traps for the weekly review, never gates a query. Draft 0.1 had it right that this is backlog tooling and not runtime.

### 4.5 Client-run deployment

Same image, client cloud, client secrets. Needs a documented install and a support model. Most of the work is the runbook.

## What is not planned

- A chat UI of our own. The chatbot the client already has is the interface. Slack is the one exception because it is also where the client already is.
- Free-text clarification. Choices are option IDs against real entities. Free text goes back through the chatbot as a new question.
- Understory changing a number. Defaults that change results are metric definitions and belong in dbt.
- A signed resolution token. Tool ordering is not user involvement, and elicitation (1.2) is the thing that guarantees the user saw the choice.

## Decision points

| When | Decide |
|---|---|
| After the second client | Whether `run_sql` is exposed to everyone or gated by role |
| After the first month of production logs | Whether pins (1.1) or verified queries (1.3) come first |
| When a client's BYO capture rate is under half | Whether that client moves to the Slack bot |
| When a client already pays for dbt Cloud | Use `DbtCloud` for that tenant, keep `MetricFlowLocal` as the default |
| Before Phase 3 | Whether the first tree is built by us or by the client's analyst with Breakdown |
