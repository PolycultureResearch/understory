# Agentic Analytics System: Architecture

Polyculture Research | Draft 0.1 | September 10, 2026

## 1. Purpose

This document describes the proposed architecture for Polyculture Research's agentic analytics system: a governed layer that lets a client's existing enterprise chatbot (Claude, ChatGPT, or an internal assistant) answer business questions against the client's warehouse through an MCP server.

It extends the current design (MetricFlow semantic layer, server-side coverage gate, `review_answer` critic, OAuth identity with gap-based sessionization, and warehouse-logged events modeled by a dbt package) with two new capabilities:

1. **Question-side ambiguity.** A structural clarification step that detects ambiguous questions from annotations declared in the semantic model and asks the user a multiple-choice question before any query runs.
2. **Explanation-side ambiguity.** A multi-candidate explanation pipeline for "why did X change" questions, built on Breakdown for localization and a business event log for candidate causes, reporting a ranked, tiered set of explanations with the unexplained share stated explicitly.

The system is not a chat interface. The client keeps its chatbot. What we build and maintain is the context the chatbot reasons over, the gates that stop it from guessing, and the evaluation loop that improves the context over time.

## 2. Design principles

**Determinism decides, the LLM renders.** Candidate metrics, ambiguity detection, numbers, and candidate explanations all come from deterministic components. The LLM parses the question into a draft spec and renders the final answer. It never decides whether a question is ambiguous and never invents an explanation that carries a magnitude.

**Fail by refusal, not by a wrong number.** Every gate returns an explicit refusal, clarification, or error rather than letting the model improvise. A plausible wrong answer is the worst outcome the system can produce.

**Surface ambiguity instead of resolving it silently.** Both kinds of ambiguity are returned as a small, ranked set of options with reasons. This mirrors how a good analyst talks to an executive: the most likely reading or explanation, the runner-up, and what would distinguish them.

**Ambiguity is declared, not inferred.** Detection is a lookup against annotations in the semantic model. This keeps behavior reproducible, testable in CI, and identical across model vendors.

**Rules live on the server.** We do not control the client's chatbot or its prompt. Anything that matters is enforced in tool contracts and tokens, not in instructions the model may ignore.

**Every refusal and clarification is backlog.** Uncovered terms, collisions that keep firing, and abandoned clarifications are logged, modeled in dbt, and reviewed as the roadmap for the client's semantic model.

**Shared core, bespoke content.** The server, engines, compiler, telemetry package, and eval harness are the same for every client. Semantic models, annotations, the metric tree, event sources, and golden sets are per client.

## 3. System overview

```
  Client chatbot (Claude / ChatGPT / internal assistant)
            │  MCP over OAuth (per-user identity)
            ▼
 ┌─────────────────────────── Polyculture MCP server ───────────────────────────┐
 │                                                                              │
 │  resolve_question ──────► Resolution engine ◄──── Annotation index           │
 │  submit_clarification ──►        │                  (compiled from dbt       │
 │                                  ▼                   semantic manifest)      │
 │                          signed resolution_id                                │
 │                                  │                                           │
 │  query_metrics ─────────► Coverage gate ──► MetricFlow ──► Warehouse         │
 │                                  └──► escape hatch: raw SQL (flagged, lower  │
 │                                       trust tier, critic mandatory)          │
 │                                                                              │
 │  explain_change ────────► Explanation engine                                 │
 │                            1. Data health checks  ◄── dbt artifacts          │
 │                            2. Localization        ◄── Breakdown (tree, BSTS, │
 │                                                        Shapley)              │
 │                            3. Event matching      ◄── Business event log     │
 │                            4. Residual + discriminating evidence             │
 │                            5. Tiered report assembly                         │
 │                                                                              │
 │  review_answer ─────────► Critic (deterministic checks, then LLM) + capture  │
 │                                                                              │
 │  Telemetry writer ──► client warehouse ──► polyculture dbt package ──► evals │
 └──────────────────────────────────────────────────────────────────────────────┘
```

## 4. Components

### 4.1 Client context layer (bespoke per client)

This is where accuracy comes from. The evidence is consistent that written-down business semantics move accuracy far more than model choice: in Cube's 2026 benchmark, a 4 KB context document lifted three frontier models by 17 to 23 points, and within each condition the models were statistically indistinguishable.

The context layer for each client contains the dbt project and MetricFlow semantic models; Polyculture annotations stored in `meta` (section 4.4); indexed dbt docs and code-derived descriptions; verified queries for frequently asked questions; pre-defined statistical measures (percentiles, ratios, rankings) that models rarely compose correctly inline; the Breakdown metric tree; the business event log sources; and the golden question sets.

### 4.2 MCP server and tool contracts (shared)

| Tool | Purpose | Gate |
|---|---|---|
| `list_metrics`, `describe_metric` | Discovery against the semantic model | None |
| `resolve_question` | Maps a question to a fully specified query spec or returns open slots | Must precede any query or explanation |
| `submit_clarification` | Applies user selections (option IDs only) to open slots | Requires a `needs_clarification` resolution |
| `query_metrics` | Executes a resolved spec through MetricFlow | Requires a valid `resolved` token; coverage gate |
| `explain_change` | Runs the explanation pipeline for a resolved metric and comparison | Requires a valid `resolved` token |
| `review_answer` | Critic and answer capture before the final answer | Returns pass or specific fixes |

**Token enforcement.** A `resolution_id` is an HMAC-signed token bound to the spec hash, the user, the session, and an expiry. `query_metrics` and `explain_change` recompute the spec hash and reject any mismatch. This prevents the model from resolving one question and then quietly querying a modified spec.

**The rendering constraint.** In a bring-your-own-chatbot architecture the server cannot force how the final answer is written. We mitigate this three ways. Tool outputs carry `required_disclosures` and tier labels as structured fields, so the correct content is always in front of the model. `review_answer` checks the draft against those fields. And answer capture gives us an audit trail, so rendering failures show up in evals rather than going unnoticed. Where the client controls the chatbot configuration (Claude projects, custom GPT instructions, an enterprise system prompt), we also ship a short instruction block, but nothing depends on it.

### 4.3 Resolution engine (shared)

The resolution engine turns a natural-language question into either a fully specified query or a structured clarification. It runs in five steps.

**Step 1: Draft.** The LLM calls `resolve_question` with the question text and a draft spec: metric, dimensions, filters, and time window, each tagged with the phrase in the question it was mapped from.

**Step 2: Independent scan.** The server scans the raw question text against the synonym index (normalized, lemmatized n-grams) without relying on the model's draft. This is the key safeguard: the model cannot bypass an ambiguous term by mapping it confidently or by leaving it out of the draft. If the scan finds "revenue" and the annotation index says "revenue" maps to two metrics, the slot opens regardless of what the model proposed.

**Step 3: Slot checks.** Each slot is checked against the annotations: metric collisions from synonyms, dimension role collisions (customer region versus store region), time semantics (anchor, window, grain, fiscal versus calendar), filter conventions, and known-unanswerable concepts.

**Step 4: Policy application.** Every annotation carries one of three policies. `apply` is for facts: the value is applied and recorded but not shown. `apply_and_disclose` is for conventions: the value is applied and added to `required_disclosures`. `ask` means the slot must be resolved by the user. User pins ("when I say revenue I mean net") are stored server-side, take precedence over client defaults, and are always disclosed.

**Step 5: Status.** The engine returns one of `resolved`, `needs_clarification`, `uncovered` (a term with no mapping, logged as backlog), `unanswerable` (a concept the client has declared out of scope, with the declared reason), or `invalid` (the draft references entities that do not exist).

**Clarification transport.** The preferred transport is MCP elicitation, which lets the server request structured input from the user directly through the client with a JSON schema, so the choice never passes through the model's paraphrase. Client support varies, so the fallback returns the options in the tool result with a render hint to present them verbatim as choices. Either way, `submit_clarification` accepts option IDs only and rejects free text. Multiple choice against real model entities is what drove the large gains in the clarification research (AmbiSQL: 42.5% to 92.5% exact match), and it keeps the answer machine-readable.

**Limits.** A clarification contains at most three open slots, ordered by declared priority. A question that would need more is returned as too broad with a suggestion to narrow it. The lexical scan only catches phrasings present in the synonym index; paraphrases it misses surface as `uncovered` terms or as eval failures, and they feed the synonym backlog.

### 4.4 Annotation schema

Annotations live under a `polyculture` namespace in `meta` so they do not depend on MetricFlow parsing them. A `polyculture compile` step reads `semantic_manifest.json` and produces the annotation index the server loads at startup.

```yaml
metrics:
  - name: net_revenue
    label: Net revenue
    description: Order revenue after discounts and returns. Used in board reporting.
    type: simple
    type_params:
      measure: net_revenue_amount
    config:
      meta:
        polyculture:
          synonyms: [revenue, sales, net sales, top line]
          priority: 1
          time:
            anchor: data_max_date        # never today()
            calendar: {value: fiscal, policy: apply_and_disclose}
            window: {policy: ask}
          dimension_roles:
            geography:
              options: [customer__region, store__region]
              policy: ask
          defaults:
            - {slot: order_status, value: excludes_cancelled, policy: apply_and_disclose}
            - {slot: currency, value: USD, policy: apply}

  - name: gross_revenue
    label: Gross revenue
    description: Order revenue before discounts and returns.
    config:
      meta:
        polyculture:
          synonyms: [revenue, gross sales, bookings]

# Client-level declarations
polyculture:
  collisions:
    - phrase: revenue
      candidates: [net_revenue, gross_revenue]
      policy: ask                        # or: prefer net_revenue, disclose
  unanswerable:
    - concept: profit by SKU
      reason: COGS is only available at category grain.
```

**CI rule.** The compiler detects every synonym that maps to more than one entity. Each collision must be explicitly adjudicated in YAML with a policy; an unadjudicated collision fails the build. This turns ambiguity handling into a reviewed, versioned artifact rather than emergent model behavior, and it is the same check for every client.

On export to OSI (Apache Ossie), synonyms and descriptions map to the spec's synonym and `ai_context` fields. Until MetricFlow parses those fields, `meta` is the source of truth.

**Example resolution object:**

```json
{
  "status": "needs_clarification",
  "resolution_id": "res_7f3a9c...",
  "question": "What was revenue in the West last quarter?",
  "spec": {
    "metric": null,
    "dimensions": [],
    "filters": [],
    "time": {
      "window": "last_fiscal_quarter",
      "resolved_range": "2026-05-01/2026-07-31",
      "anchor": "data_max_date",
      "grain": "quarter"
    }
  },
  "applied_defaults": [
    {"slot": "time.calendar", "value": "fiscal", "policy": "apply_and_disclose"}
  ],
  "open_slots": [
    {
      "slot": "metric",
      "phrase": "revenue",
      "priority": 1,
      "options": [
        {"id": "net_revenue", "label": "Net revenue", "hint": "After discounts and returns. Used in board reporting."},
        {"id": "gross_revenue", "label": "Gross revenue", "hint": "Before discounts and returns."}
      ]
    },
    {
      "slot": "dimension_role.geography",
      "phrase": "the West",
      "priority": 2,
      "options": [
        {"id": "customer__region", "label": "Where the customer is located"},
        {"id": "store__region", "label": "Where the order was fulfilled"}
      ]
    }
  ],
  "required_disclosures": ["Quarters are fiscal (February to January)."],
  "uncovered_terms": []
}
```

### 4.5 Query path and coverage gate (existing, extended)

A resolved spec compiles through MetricFlow and executes in the warehouse. The coverage gate decides server-side whether the spec is expressible in the semantic layer. If it is not, the request is either refused with a backlog entry or routed to the escape-hatch raw SQL path, depending on client configuration and user role. Escape-hatch answers carry a lower trust tier, make `review_answer` mandatory, and must be labeled as not coming from governed metrics.

Every result includes provenance: metric definitions used, compiled SQL hash, data freshness, coverage path, and applied defaults.

### 4.6 Explanation engine (shared, built on Breakdown)

The explanation engine answers "why did X change" questions. Its core rule is that the LLM does not generate explanations with magnitudes. Candidates come from two deterministic sources: Breakdown localizes where the change happened, and the business event log supplies candidate causes. The LLM is limited to phrasing and to a clearly labeled speculative tier.

**Inputs.** A resolved metric, a target window, and a comparison baseline. The baseline (prior period, same period last year, or forecast) is itself an annotated slot, so it is either defaulted with disclosure or asked.

**Stage 1: Data health.** Before any business explanation, check freshness against SLA, dbt test failures on models upstream of the metric (from `run_results.json` and manifest lineage), row-count and null-rate anomalies on upstream sources, and recent backfills or model changes from deploy history. A blocking issue leads the report and marks everything below it as provisional. The mundane explanation is right often and is the most costly one to miss.

**Stage 2: Localization.** Breakdown walks the client's metric tree. The BSTS counterfactual gives expected versus actual with an interval, and Shapley attribution assigns each branch a contribution with an interval and an estimated change-point date. Branches below a materiality threshold are grouped as "other."

**Stage 3: Event matching.** For each material branch, events in the business event log are scored on three deterministic criteria: temporal fit (event start relative to the branch's change point, allowing for the event's declared expected lag), scope fit (overlap between the event's declared scope and the branch's dimensions), and direction fit (the event's declared expected direction versus the observed change). The result is a match strength, not a causal probability, and it is labeled that way.

**Stage 4: Residual and discriminating evidence.** The residual is the share of the change not accounted for by the model's components and matched events, reported with its interval. For the top two candidates, the engine attaches the evidence that would distinguish them, drawn from templates keyed on candidate type: a price change versus a traffic-mix shift points to conversion at user grain; a pipeline issue versus a real change points to reconciliation against the source system; two overlapping events point to an experiment or a holdout region. When the distinguishing evidence is not in the data, the report says so explicitly.

**Stage 5: Report assembly.** Candidates are ranked by attributed contribution. The report is tiered:

| Tier | Source | What it may claim | Rendering rule |
|---|---|---|---|
| Data issues | Health checks | The numbers may be wrong or incomplete | Always first when present |
| Quantified | Breakdown | Where the change happened and how much | Magnitude with interval; no causal language |
| Corroborated | Quantified branch plus matched event | Timing and scope are consistent with a known event | "Consistent with," never "caused by" |
| Speculative | LLM | Possible factors not present in our data | Labeled, no magnitudes, only against the residual |
| Unexplained | BSTS residual | Share of the change nothing in our data accounts for | Always stated |

**Business event log.** A `fct_business_events` model in the Polyculture dbt package, fed by connectors where possible (GitHub or deploy releases, Shopify or Stripe price changes, marketing calendars, dbt incidents) and a maintained seed file for the rest. Schema: `event_id`, `event_type` (release, price_change, promo, campaign, seasonality, pipeline_incident, external), `start_at`, `end_at`, `scope` (dimension filters), `expected_direction`, `expected_lag`, `source`, `owner`. The matching logic is shared across clients; only the sources are bespoke.

**Example rendered answer** (what the LLM produces from the report):

> Net revenue was down 8% last week versus the same week last year (range 6% to 10%). Most of the drop, about 60%, came from repeat customers on mobile in the Northeast, and its timing and scope line up with the shipping price change on September 14. A smaller share, about 15%, came from new-customer volume across all regions, which matches the end of the summer paid social campaign. Roughly 25% isn't explained by anything in our data. To separate the shipping effect from the campaign, the next step is comparing checkout conversion at the user level before and after the 14th. Note: data is fresh through September 9, and revenue here excludes cancelled orders.

### 4.7 Critic: `review_answer` (existing, extended)

The critic runs deterministic checks first and an LLM review second. Deterministic checks: every number in the draft traces to a tool output in the session (with rounding tolerance); every item in `required_disclosures` is present; tier order is preserved; escape-hatch provenance is labeled. The LLM review checks for causal language attached to quantified or corroborated tiers, speculative content presented as fact, and answers to questions the resolution marked unanswerable. It returns pass or a list of specific fixes, and it captures the final answer for evaluation.

### 4.8 Identity, sessions, and telemetry (existing, extended)

Per-user OAuth identity passes through to the warehouse so row-level permissions are enforced where the data lives. Sessions are defined server-side by inactivity gaps. The telemetry writer emits `question_received`, `resolution_emitted` (status, open slots, collision IDs, uncovered terms), `clarification_answered`, `clarification_abandoned`, `query_executed` (coverage path), `explanation_emitted` (tiers, residual share, health flags), `review_result`, and `answer_captured`. Events land in the client's warehouse and the Polyculture dbt package models them into `fct_questions`, `fct_clarifications`, `fct_explanations`, and the eval marts.

### 4.9 Evaluation loop

Each client has two golden sets. The question set is built from real stakeholder asks, and each item records the expected resolution (including whether it should trigger a clarification and which slots) and the expected answer. The explanation set is built from past incidents with known causes, taken from post-mortems or from people who remember. The harness runs against the live MCP server with a pinned model on every dbt deploy and on a weekly schedule.

| Metric | What it tells us |
|---|---|
| Coverage rate | Share of real questions the semantic layer can answer |
| Resolution accuracy | Asked when it should, did not ask when it should not |
| Answer accuracy | Correct numbers on resolved golden questions |
| Clarification rate and abandonment | Whether clarifications help or annoy |
| Refusal rate by reason | Uncovered versus unanswerable versus invalid |
| Top-two explanation hit rate | Known cause appears among the top two candidates |
| Residual share trend | Whether the event log and tree are getting more complete |
| Event-match precision | Matched events that turn out to be relevant |

A weekly backlog review takes the top uncovered terms, the collisions firing most often, the most-abandoned clarifications, and critic failures, and turns them into annotation or model changes, which go through CI and trigger an eval rerun.

## 5. Request flows

**Covered descriptive question.** "What was net revenue by channel last month?" The scan finds no collisions; time and calendar resolve from hard defaults; the engine returns `resolved` with a token; `query_metrics` executes through MetricFlow; the model renders; `review_answer` passes. One round trip, no user friction.

**Ambiguous question.** "What was revenue in the West last quarter?" The scan finds "revenue" (two metrics) and "the West" (two geography roles). The engine returns `needs_clarification` with two slots and discloses the fiscal calendar. The user picks net revenue and customer region through elicitation; `submit_clarification` returns `resolved`; the query runs. If the user pins net revenue, the next question skips that slot and shows it as a disclosure.

**Why question.** "Why did revenue drop last week?" Resolution applies the user's net revenue pin and the default baseline (same week last year), both disclosed. `explain_change` runs health checks (clean), Breakdown localization, event matching, and residual calculation, and returns the tiered report. The model renders the answer in section 4.6, and the critic confirms that every number traces to the report and that no causal verb is attached to the corroborated tier.

## 6. Productization split

| Shared core (built once) | Per client (bespoke) |
|---|---|
| MCP server and tool contracts | Semantic models and annotations |
| Resolution engine and token signing | Collision and unanswerable adjudications |
| Annotation compiler and CI check | Breakdown metric tree |
| Coverage gate and escape-hatch handling | Event source configuration and seed events |
| Explanation pipeline and event matcher | Golden question and incident sets |
| Data health checks on dbt artifacts | Identity passthrough and warehouse dialect |
| Critic and answer capture | dbt docs indexing |
| Telemetry dbt package and eval marts | Materiality and match thresholds |
| Eval harness | |
| Common event connectors | |

## 7. Failure modes and mitigations

| Failure mode | Mitigation |
|---|---|
| Plausible wrong number from ad hoc SQL | Semantic-layer path by default; escape hatch flagged, lower trust, critic mandatory |
| Model skips an ambiguous term | Independent server-side lexical scan; token binds spec hash |
| Silent convention (fiscal calendar, cancelled orders) | `apply_and_disclose` policy; disclosure presence checked by critic |
| Time anchored to today instead of the data's last date | `anchor: data_max_date` declared per metric |
| Answering an unanswerable question | Declared `unanswerable` concepts with reasons |
| Clarification fatigue | Three-slot cap, priority ordering, user pins, abandonment tracked |
| One confident explanation when several fit | Deterministic candidates, ranking by attribution, top-two with distinguishing evidence |
| Fluent causal narrative | Tier rules; causal language checked by critic; residual always stated |
| Data problem mistaken for business change | Health checks run first and lead the report |
| Curation rot | Collision CI, telemetry-driven backlog, eval on every deploy |

## 8. Build sequence

Phase 1 extends what already exists: the annotation schema and compiler with the collision CI check, the resolution engine, `submit_clarification`, and token-gated `query_metrics`. Phase 2 adds the telemetry events and eval marts, and rebuilds the golden set so each item records its expected resolution. Phase 3 builds the explanation engine: health checks on dbt artifacts, the Breakdown integration behind `explain_change`, and the business event log model with a seed-file workflow. Phase 4 adds event connectors, the critic's tier checks, MCP elicitation support, and OSI export.

## 9. Open questions

Hosting model: per-client deployment inside the client's cloud versus a multi-tenant Polyculture service, which affects identity passthrough, data residency, and pricing. Semantic layer runtime: dbt Semantic Layer (billing and permissions remain unresolved) versus self-hosted MetricFlow. Elicitation support across the chatbots clients actually use, and how good the fallback is in practice. Who owns event log maintenance at the client, and what happens to explanation quality when it lapses. How to set materiality and match thresholds without overfitting to the incident set. Whether non-technical users should see the escape hatch at all. Whether an embedding-based detector should run alongside the lexical scan, used only to flag likely missed synonyms for the backlog, never to gate.

## 10. Evidence basis

- Cube, *Semantic Layers for Reliable LLM-Powered Data Analytics* (2026): https://arxiv.org/pdf/2604.25149
- dbt Labs, Semantic Layer vs. text-to-SQL benchmark (2026): https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026
- AmbiSQL, clarification for ambiguous text-to-SQL: https://arxiv.org/abs/2508.15276
- BIRD-Interact, interactive and ambiguous settings (ICLR 2026): https://arxiv.org/abs/2510.05318
- OpenAI, *Inside our in-house data agent* (January 2026): https://openai.com/index/inside-our-in-house-data-agent/
- Gartner, Market Guide for Agentic Analytics (February 2026): prediction that 60% of MCP-only agentic analytics projects will fail by 2028 without a consistent semantic layer
