# Understory roadmap

Polyculture Research | September 12, 2026

The design (`understory-mvp-design.md`, draft 0.3) answers descriptive questions through a governed semantic layer, prefers and discloses when a declared trap fires, asks only where the client chose to, falls to labeled SQL when the semantic layer cannot answer, and records every uncovered question as a gap. Everything deferred is here, in the order it should be built, with the reason it was deferred and what has to be true before it starts. The design's section 14 holds the near-term build sequence; this document starts after it.

Phases are sequential by default. Items within a phase are independent unless noted.

## Phase 1: harden the loop

Goal is to make the MVP trustworthy at two or three clients before adding capability.

### 1.1 User pins

"When I say margin I mean the rate." A pin is a stored clarification choice per `user_hash` and trap. The next question skips the ask and adds a disclosure instead. With `prefer` as the norm there are few asks left to pin, so pins matter less than draft 0.2 expected, but a user who answers the same ask twice will still want one.

Deferred because pins are the first persistent per-user state and the write-only log design has no read path. Pins need a small mutable store outside the log, keyed by `user_hash`, with an unpin tool. Build when the abandonment rate on a tenant's asks says so.

### 1.2 MCP elicitation

Where the chatbot supports elicitation, `query_metrics` requests the clarification through the client with a JSON schema, so the choice never passes through the model's paraphrase. Fallback stays as it is in the MVP.

Deferred because support across ChatGPT Enterprise and Claude.ai is uneven and the fallback works. Revisit when either platform ships it for custom connectors.

### 1.3 Verified queries

A per-tenant set of saved specs for questions asked often, matched by phrase and returned with a `verified: true` provenance flag. These are the semantic layer's "known good" answers and they turn the most frequent questions into a fast path.

Depends on a few weeks of production logs to know which questions recur. It is also the first feature that would need the server to read something derived from its own history at runtime, which the write-only design forbids. The saved specs would have to be promoted into the tenant directory by an analyst, the same way gaps are promoted to golden items, so the server reads config and never the log. Decide that explicitly before building.

### 1.4 Freshness and test status in provenance

Read `run_results.json` and `sources.json` from the dbt deploy and attach upstream test failures and source freshness to every result. A failing test upstream of a metric is disclosed with the result. This is the cheap half of the data health checks in draft 0.1 and it does not need the explanation engine.

### 1.5 Optional LLM critic

An LLM pass inside `log_answer`, through OpenRouter, that checks the draft for causal language, speculation presented as fact, and answers to refused questions. Off by default because it costs money per answer and cannot be enforced in a BYO chatbot. On for the harness and for tenants who ask.

### 1.6 Second chatbot integration

Whichever of Claude.ai or ChatGPT Enterprise the first client does not use. The tool contracts do not change. What changes is connector auth, instruction placement, and the eval capture rate, which we report per platform.

### 1.7 Token cost

The first live eval cost about 6 cents per question on Claude Sonnet 5, with 80% of it in input tokens and most of that a fixed prefix (system prompt, tool schemas, the get_context result) re-sent on every request without caching. `knowledge/token-use-2026-09-11.md` has the measurements. Prompt caching, conversation continuation on clarification, usage capture, and budget guards are being done now on the `harness-cost` branch. What remains:

- Run routine evals on a cheaper model (Haiku 4.5 or GPT-5 mini) and Sonnet weekly. The harness score is a floor for the client's chatbot either way.
- Trim payloads the chatbot re-reads on every turn. Drop the full dimension list from `get_context` (it pushed the document from the 3 to 6 KB target to 7.6 KB, and `describe_metric` carries it). Return compiled SQL only on request; it is half of every governed result. Lower the chat row cap; a 200-row result is about 3,300 tokens that ride along for the rest of the conversation.
- Stop the redundant `list_metrics` call. In 11 of 47 items the model called it right after `get_context`, which already lists every metric. Say so in the tool description.

These matter more in production than in evals: the client's chatbot pays those tokens, and their employees feel the latency.

### 1.8 Resolved-question candidates

If clarification proves onerous, a tool that takes the user's question and returns one or more fully resolved readings for the user to pick from, instead of a bare list of options. This is the first place Understory would run a model inside a tool call, at our cost through OpenRouter, so it is off by default and a tenant setting.

Triggered by the abandonment rate: asks returned with no resubmission. The metric ships in the design so the trigger is observable before the feature exists. The same mechanism, once built, is the natural home for parsing or summarizing documents the context layer needs.

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

### 4.4 Gap clustering and synonym suggestion

Runs offline over the `gaps` family. Groups gap records whose phrasings differ but whose missing entity or SQL is the same, proposes synonyms and traps for the weekly review, and never gates a query. The design keys gaps on what was missing, which is a stable key the data team can act on; clustering sits on top of that key rather than replacing it. Draft 0.1 had it right that this is backlog tooling and not runtime.

### 4.5 Flywheel analytics

Tools over the backlog that tell a data team where to spend the next sprint: gaps by count and distinct users, coverage trend per tenant, time from first gap to closed, and which closed gaps are now answered most. The first version is the dbt marts; a later version is a report the analyst can hand to the client.

### 4.6 Client-run deployment

Same image, client cloud, client secrets. Needs a documented install and a support model. Most of the work is the runbook.

## What is not planned

- A chat UI of our own. The chatbot the client already has is the interface. Slack is the one exception because it is also where the client already is.
- Free-text clarification. Choices are option IDs against real entities. Free text goes back through the chatbot as a new question.
- Understory changing a number. Defaults that change results are metric definitions and belong in dbt.
- A signed resolution token. Tool ordering is not user involvement, and elicitation (1.2) is the thing that guarantees the user saw the choice.
- Asking for a time window. A missing window takes the tenant's preferred default and is disclosed.
- The server reading its own log at runtime. Anything the server needs from its history is promoted into the tenant directory by an analyst first.
- A model call in the core path. The deterministic path stays free of LLM calls so the cost claim holds by construction. Model calls are optional features (1.5, 1.8, 4.4) that a tenant turns on.

## Decision points

| When | Decide |
|---|---|
| After the first friendly users | Where the gentle guidance lives: trap hints, `prefer` disclosures, or both |
| After the second client | Whether `run_sql` is exposed to everyone or gated by role |
| After the first month of production logs | Whether pins (1.1) or verified queries (1.3) come first |
| When a tenant's abandonment rate is high enough to notice | Whether to build resolved-question candidates (1.8) |
| When the missing-entity key over-splits or lumps gaps | Whether to build clustering (4.4) |
| When a client's BYO capture rate is under half | Whether that client moves to the Slack bot |
| When a client already pays for dbt Cloud | Use `DbtCloud` for that tenant, keep `MetricFlowLocal` as the default |
| Before Phase 3 | Whether the first tree is built by us or by the client's analyst with Breakdown |
