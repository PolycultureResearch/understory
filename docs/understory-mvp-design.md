# Understory: design

Polyculture Research | Draft 0.3 | September 12, 2026

Supersedes draft 0.2 (September 10) and sections 3 through 8 of `agentic-analytics-architecture.md` (draft 0.1). The roadmap in `understory-roadmap.md` holds everything deferred. Vocabulary is defined in `CONTEXT.md` at the repository root and decisions that were hard to reverse are in `docs/adr/`. This document uses the glossary's terms without redefining them.

## 1. What Understory is

Understory is the natural-language interface to a client's analytics. The client's employees ask questions in the chatbot they already use (ChatGPT Enterprise, Claude, or later a Slack bot). Understory is the MCP server that chatbot talks to. It exposes the client's dbt semantic layer as tools, prefers a declared reading and says so when a phrase is ambiguous, asks only where the wrong reading would mislead, refuses when the data cannot answer, shows where every number came from, and records every question it could not answer as a gap the data team can close.

The billable work at each client is the dbt data model and semantic layer. Understory is what makes that work usable through chat within days of finishing it.

What the client is buying, in order:

1. Confidence. Understory never returns a plausible wrong number. It prefers and discloses, asks, or refuses.
2. The gap flywheel. Questions the semantic layer cannot answer become the data team's ranked backlog, with the ad hoc SQL that answered them as a draft of the metric to build. Common gaps become the roadmap, and closed gaps become regression tests.
3. Owning the context layer. The context document, traps registry, and golden sets live in the client's own dbt repository, next to the semantic layer they describe.

Cost is a footnote, not the pitch. The chatbot the client already pays for does the parsing and rendering. Understory's own path is deterministic and free of model calls. Where Understory does run a model (evals, demos, the Slack transport, and optional roadmap features) the first live eval measured about six cents per question on Claude Sonnet 5 before prompt caching, with input tokens as 80% of the bill (`knowledge/token-use-2026-09-11.md`). The model is a tenant setting. The realistic competitor is the agentic feature in the BI tool the client already pays for, and the human data team's queue.

Three constraints shape everything below.

The chatbot is the client's. We do not control its prompt, its model, or whether it calls our tools in the order we would like. Anything that matters is enforced inside a tool call or not at all.

The semantic layer is the source of truth for every number. Understory never changes a number. It maps words to entities, decides when to ask, and discloses what it applied.

Boring tools. Python, DuckDB, Parquet, dbt, MetricFlow. Nothing that needs a platform team to run.

## 2. Principles

- Deterministic components decide. The LLM parses and renders. The core path makes no model calls.
- Refuse or ask rather than return a plausible wrong number. This is non-negotiable.
- Prefer and disclose is the norm. Ask is the exception, and every ask has a stated reason the client's data owner has signed off on. "Did you mean the last thirty days or the trailing thirty days" is a failure, not rigor.
- Guide the user gently. Disclosures and hints teach the vocabulary so the next question is sharper, without a round trip.
- Ambiguity is declared in config and checked in CI, not inferred at runtime.
- Every uncovered question is a gap, and every gap is backlog for the semantic layer. Understory records why it could not answer, not only that it could not.
- Understory is write-only on its own history. The server appends to the log and cannot read it. Section 8 explains why.
- Shared core, bespoke content. The core is tested on four synthetic companies. Each client's content is tested by that client's golden sets.

## 3. System overview

```
  ChatGPT Enterprise / Claude.ai            Understory harness (Python, OpenRouter)
           │  MCP over HTTPS                        │  same tools, in-process
           ▼                                        ▼
 ┌────────────────────── Understory server (one container per tenant) ──────────────────────┐
 │                                                                                          │
 │  get_context ─────────┐                                                                  │
 │  list_metrics ────────┤                                                                  │
 │  describe_metric ─────┼──► Catalog  ◄── semantic_manifest.json (dbt parse)              │
 │  search_dimension_values ┘                                                               │
 │                                                                                          │
 │  query_metrics ──► Traps check ──► SemanticLayer.compile(spec) ──► Warehouse.run(sql)    │
 │        │               │                (MetricFlow, local or Cloud)   (DuckDB, BigQuery) │
 │        │          needs_clarification                                                    │
 │        │          unanswerable / invalid  ──► gap record                                 │
 │                                                                                          │
 │  run_sql(sql, question, reason) ──► SQL guard ──────────────────► Warehouse.run(sql)     │
 │                     (read-only, row cap, timeout, ungoverned disclosure) ──► gap record   │
 │                                                                                          │
 │  log_answer ──────► Number check (every number in draft traces to a result this session) │
 │                                                                                          │
 │  Telemetry writer ──► append-only Parquet in three families ──► object store ──► dbt     │
 │                       events / text / gaps                                               │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
                                                              │
                                     dbt package ──► mart_semantic_backlog ──► gap promotion
                                                     (ranked gaps)             (draft golden items)
```

The harness on the right is ours. It runs the same tools in-process with a pinned model through OpenRouter. It exists so we can run evals without depending on a client's chatbot, demo without a client account, and later become the Slack transport. It is part of the product because the eval loop is not optional, and because it is the only path where the full loop is enforced.

## 4. Tools

Seven tools. Each returns structured JSON with a `provenance` block and, where relevant, `required_disclosures`.

| Tool | Purpose | Returns |
|---|---|---|
| `get_context` | The client's business semantics in a few kilobytes | Markdown: what the company does, metric conventions, fiscal calendar, known gotchas, current data freshness |
| `list_metrics` | Discovery | Name, label, description, type, available dimensions, synonyms |
| `describe_metric` | One metric in depth | Definition, expression, filters baked in, upstream models, freshness, example questions |
| `search_dimension_values` | Resolve "the West" to real values | Matching values for a dimension, with counts |
| `query_metrics` | Execute a metric spec | Rows, compiled SQL hash, applied conventions, disclosures. Or `needs_clarification`, `unanswerable`, `invalid` |
| `run_sql` | Escape hatch, with the reason it was needed | Rows with `governed: false`, the SQL that ran, and a disclosure naming the reason |
| `log_answer` | Capture the draft answer, check numbers | `pass` or a list of numbers with no source |

### 4.1 `get_context`

The tool that moves accuracy most and costs least to build. The Cube benchmark cited in draft 0.1 found a 4 KB context document moved accuracy more than model choice did. The document is a Markdown file per client, written by us during the engagement, plus a generated tail. The generated tail lists the metrics by category, the fiscal calendar if any, and the data freshness computed at request time.

Chatbots do not reliably call a context tool first. The harness always does. For BYO chatbots we also ship the same content as connector instructions where the platform allows it, and `list_metrics` repeats the conventions section in its response so a chatbot that skips `get_context` still sees it.

The context document, the tool descriptions, and the connector instructions are the only prompt surfaces we control in a client's chatbot. They are versioned like code and every wording change runs both eval sets before and after (section 10).

### 4.2 `query_metrics`

Input is a spec, not a question. The chatbot does the parsing.

```json
{
  "metrics": ["net_revenue"],
  "group_by": ["order__category"],
  "where": [{"dimension": "order__country", "op": "in", "values": ["US", "CA"]}],
  "time": {"grain": "month", "start": "2026-03-01", "end": "2026-08-31"},
  "question": "net revenue by category in North America over the last six months",
  "clarifications": [{"trap": "revenue", "choice": "net_revenue"}]
}
```

The server runs the following in order.

1. Validate the spec against the catalog. Unknown metric or dimension returns `invalid` with the nearest matches, and writes a gap record naming what was missing and what was suggested.
2. Run the traps check (section 5) against both the spec and the question text. A `prefer` trap applies its candidate and adds a disclosure. An `ask` trap that is not satisfied by a `clarifications` entry returns `needs_clarification` with options. An `unanswerable` match returns that status with the declared reason and writes a gap record.
3. Resolve time. `end` defaults to the metric's latest available date, never today. A missing window takes the tenant's default window. Both are disclosed, and the disclosure names how to ask for a different window.
4. Compile through the semantic layer, execute through the warehouse adapter, cap rows.
5. Return rows plus provenance and disclosures. Log the event.

There is no separate resolve step and no signed token. The traps check is deterministic and cheap, so running it inside `query_metrics` costs nothing and removes two round trips and a session-state mechanism.

### 4.3 `run_sql`

The semantic layer will be young at every new client. SQL will carry a lot of early questions, and that is fine as long as the answer says so and the gap is recorded. `run_sql` is a first-class path with its own guard, and it is the main source of gaps.

- Takes `sql`, the user's `question`, and a one-line `reason` the governed path could not answer it ("no promo_code dimension on orders"). All three are required. The model knows the reason at the moment it falls back and it costs nothing to ask.
- Read-only. The guard parses with sqlglot and rejects anything other than a single SELECT.
- Row cap and statement timeout from tenant config.
- Schema scope from tenant config. Only the mart and semantic schemas, never raw or staging by default.
- Response carries `governed: false`, the SQL that ran, the tables it touched, and a required disclosure of the shape "answered with ad hoc SQL because <reason>; not a governed metric". The reason reaches the user through the same mechanism as every other disclosure.
- Every call writes a gap record (section 8.3) with the question, the reason, the SQL, and the tables touched.

`describe_table` and `list_tables` are not separate tools. `get_context` names the tables `run_sql` may touch and `run_sql` accepts `information_schema` queries.

### 4.4 `log_answer`

The chatbot sends its draft answer before showing it. The server extracts numbers from the draft and checks each against the results returned earlier in the conversation, with rounding tolerance. It returns `pass` or the list of numbers with no source. It also captures the draft for the log, including drafts that refuse without ever having run a query, so prose refusals are recorded.

This tool is advisory in a BYO chatbot. Nothing forces the model to call it. We measure the call rate per tenant as an eval metric and accept that capture is partial. The harness always calls it. There is no LLM critic in the MVP; it is roadmap 1.5.

## 5. Traps registry

Anything that changes a number belongs in the semantic layer. Anything about how to resolve a question belongs in the traps registry.

| Belongs in dbt / MetricFlow | Belongs in `traps.yml` |
|---|---|
| Metric definitions, filters, measures | Which phrases collide, and what to do about it |
| Descriptions and labels | Which concepts are declared out of scope, and why |
| Synonyms (in `meta` until OSI lands) | Which dimension roles collide |
| Fiscal calendar as time dimensions | Whether to prefer or ask, and the default window |
| Currency, cancelled-order handling | Priority order when several traps fire |

### 5.1 Policies

Three policies. `prefer X` applies X and adds a disclosure that names the alternative. `ask` returns options with hints and stops. `disclose` applies whatever the spec said and adds a disclosure. There is no silent `apply`, because the number-changing defaults that needed it live in the metric.

`prefer` is the norm. `ask` is the exception and must carry a `why`. The registry check fails an `ask` without one. During an engagement Polyculture owns the list of asks, then hands it to the client's data owner, who signs off on each because each is a friction point the client is choosing. A typical client has one or two.

### 5.2 Example

A registry for a retail client, using metrics from the `retail_dtc` vertical in fake_companies, where `gross_revenue` and `net_revenue` both exist.

```yaml
version: 1

collisions:
  - phrase: [revenue, sales, top line]
    candidates: [net_revenue, gross_revenue]
    policy: prefer net_revenue
    hint:
      net_revenue: "After discounts and refunds. Used in board reporting."
      gross_revenue: "Before discounts and refunds."

  - phrase: [margin]
    candidates: [gross_margin, margin_rate]
    policy: ask
    why: "Finance reports margin in dollars, marketing in percent, and the two have been confused in board decks."

dimension_roles:
  - phrase: [region, west, east, country]
    candidates: [customer__country, order__country]
    policy: prefer order__country
    disclose: "Country is where the order shipped, not where the customer lives."

conventions:
  - name: time_anchor
    value: latest_available_date
    disclose: true
  - name: default_window
    value: trailing_30_days
    policy: prefer

unanswerable:
  - phrase: [profit by sku, sku margin]
    reason: "COGS is only available at category grain."
  - phrase: [ltv, lifetime value]
    reason: "No LTV model yet. Ask about repeat order share instead."
```

### 5.3 Windows

The default window is a tenant setting, preferred and disclosed, never asked. The server ships a fixed vocabulary (trailing 7, 30, and 90 days, last full month, last full quarter, year to date) and a tenant may define more by name with a start and end rule. Fiscal periods are not window definitions; they come from the semantic layer's time dimensions, where the client already defined them once.

### 5.4 Disclosures as guidance

A `prefer` disclosure says what was applied and how to get the alternative: "'sales' is read as net revenue, after discounts and refunds; ask for gross revenue for the figure before them." The hint strings supply the wording. Whether the hints, the disclosures, or both carry the teaching load is a user-experience decision deferred until real users have seen both (roadmap decision points).

Disclosures derive from provenance where possible. If the resolved time dimension is a fiscal quarter, the disclosure says so from the dimension's own description. The `disclose` strings in the registry are for conventions that have no entity to hang off.

### 5.5 Matching and CI

Matching is a normalized whole-phrase match over the question text and the spec, with a candidate's own name masked so "net revenue" does not trip the revenue collision. No lemmatized n-gram index. The chatbot's model is already good at mapping "revenue" to a metric when the descriptions are good. The registry exists to catch the handful of declared traps where a confident mapping is the wrong outcome.

The compile step runs in CI. It reads `semantic_manifest.json` and `traps.yml`, checks every candidate exists, checks every `ask` has a `why`, and detects synonyms declared in `meta` that map to more than one metric. An unadjudicated collision fails the build.

## 6. Semantic layer and warehouse

### 6.1 The lock-in question

dbt's own MCP server is not the answer. Its semantic layer tools all need dbt Cloud, and the tool set is discovery plus `query_metrics` with none of the gating above. Understory needs its own server regardless. The question is only what compiles a metric spec to SQL.

Open-source MetricFlow is the default, behind an interface so dbt Cloud is a config switch for clients who already pay for it.

```python
class SemanticLayer(Protocol):
    def catalog(self) -> Catalog: ...                     # from semantic_manifest.json
    def compile(self, spec: MetricSpec) -> CompiledQuery: ...   # SQL + hash
    def dimension_values(self, dim: str, q: str) -> list[Value]: ...

class Warehouse(Protocol):
    def run(self, sql: str, *, timeout: int, row_cap: int) -> Result: ...
    def latest_date(self, relation: str, column: str) -> date: ...
```

`MetricFlowLocal` runs `mf query --explain` against the client's dbt project to get SQL and hands that SQL to the `Warehouse` adapter. It needs dbt-core and the warehouse adapter in the container, which is why the semantic layer runs in its own process from the MCP server. Latency is a few seconds per compile. Compiled SQL is cached by spec hash.

`DbtCloud` calls the Semantic Layer API through `dbt-sl-sdk`. Same interface, no local dbt install.

Executing the SQL ourselves rather than letting MetricFlow execute it gives one connection, one provenance format, and one row cap for both the governed path and `run_sql`.

### 6.2 Warehouse adapters

`DuckDB` for development, tests, and the fake_companies tenants. `BigQuery` because that is where the first client is. Both use a single service account or local file. Per-user warehouse identity is out of scope, see section 8.

Parquet is the interchange format for everything Understory writes: telemetry, cached results, eval fixtures.

## 7. Tenants and hosting

One container image, one running container per tenant, configuration mounted. Multi-tenancy inside a process is a source of bugs we do not need to invite for a handful of clients.

A tenant is a directory, and it lives in the client's dbt repository next to the project it describes (ADR 0001). The container mounts it and reads it. A change to a metric, its trap, and its golden question is one pull request the client reviews. The four fake_companies tenants stay in this repository as fixtures.

```
<client dbt repo>/understory/
  tenant.yml            # warehouse connection, semantic layer mode, row caps, schema scope, default window
  context.md            # the get_context document
  traps.yml             # section 5
  semantic_manifest.json  # produced by the dbt deploy, or a path to the dbt project
  golden/
    traps.yml           # the trap set, section 10
    realistic.yml       # the realistic set, section 10
```

Hosting is Polyculture-run for the MVP, one small container per tenant on Cloud Run or a VM, reachable over HTTPS. Clients who require in-cloud deployment get the same image.

### 7.1 Connector authentication

ChatGPT Enterprise and Claude.ai both add custom connectors as remote MCP servers over HTTPS. Both expect OAuth 2.1 at the connector level. This is authentication of the chatbot to Understory and is separate from warehouse identity.

The MCP server validates tokens issued by the client's identity provider. Understory never stores passwords and never maps a user to warehouse credentials. The only thing it takes from the token is a stable subject identifier, which section 8 hashes before it reaches any log.

Verify the exact connector auth options against the ChatGPT Enterprise admin docs before the first deployment.

## 8. Identity, logging, and privacy

### 8.1 Single service account

The server connects to the warehouse with one read-only service account per tenant, scoped to the mart and semantic schemas. Per-user OAuth passthrough is roadmap 2.3. Every user of the connector sees what the service account sees. Tenant config states this and the data owner signs off on which schemas are in scope.

### 8.2 Who asked what

Every event carries `user_hash`, an HMAC of the subject identifier from the connector token keyed with a per-tenant secret. The secret lives in the server's secret store and never in the warehouse. A reader of the log sees that the same person asked twelve questions, not who they are.

### 8.3 Write-only, in three families

Question text is sensitive. The server has no read access to its own history, and the log is split so most readers never see text.

The telemetry writer appends Parquet files to a log prefix. Locally that is a directory. In BigQuery deployments it is a GCS prefix where the server's log identity holds `objectCreator` only. The warehouse reads the prefix through an external table or a scheduled load, and the dbt package models it from there.

| Prefix | Contains | Readers |
|---|---|---|
| `log/events/` | Timestamps, `user_hash`, tool name, status, metric and dimension IDs, trap IDs, row counts, SQL hash, latency | The client's analytics team and Polyculture |
| `log/text/` | Question text, spec, compiled SQL, draft answer, keyed by `event_id` | Polyculture and the client's named data owner |
| `log/gaps/` | Gap records: question text, the spec tried, what was missing, the fallback reason, the ad hoc SQL and tables touched. No `user_hash`. | The client's data team |

Gaps are their own family because the flywheel's consumers are the data team, and the data team should not need `text` access to do their job (ADR 0002). A gap is de-identified by construction. It may carry SQL literals, which only reach mart schemas its readers already have; the data owner's sign-off says so.

The dbt package builds `fct_questions`, `fct_clarifications`, `fct_refusals`, and the eval marts from `events`. It builds `mart_semantic_backlog` from `gaps`, grouped on what was missing (the absent metric or dimension, or the tables the SQL touched) with example phrasings, occurrence count, and distinct-user count from `events`. Phrase-level clustering is roadmap 4.4. Marts over `text` exist for the weekly review and live in a restricted schema. Retention on `text` is a tenant setting, default 90 days.

Two service accounts per tenant. The query identity reads marts and cannot touch the log prefix. The log identity creates objects under the log prefix and can read nothing.

### 8.4 Conversations and sessions

The server keeps a small in-memory conversation per MCP connection so `log_answer` can check numbers against earlier results. Conversations expire after inactivity and nothing in them is persisted beyond the events already logged. Sessions, for analysis, are reconstructed in dbt from timestamps and `user_hash`.

## 9. The harness

A Python agent that talks to the same tools in-process, with the LLM behind OpenRouter through Pydantic AI. The model is a tenant setting and evals pin it. The default is a mid-tier model chosen for cost per eval run, so that a model that does well on our evals is a floor for the client's chatbot rather than a ceiling.

The harness is the reference chatbot, and it has to be cheap because it becomes the Slack transport. Prompt caching of the fixed prefix, message history across the clarification turn, per-question usage and cost capture, and budget guards landed with PR #2. What remains, payload trimming and a cheaper routine eval model, is roadmap 1.7.

The harness has four jobs.

- Run both golden sets on every dbt deploy and weekly, recording resolution status, numbers, disclosures, `log_answer` result, and cost per question.
- Run in BYO mode: the same agent with its own system prompt removed, seeing only the surfaces a client's chatbot sees (connector instructions, tool descriptions, `get_context`). This is an approximation of a real connector and is paired with a manual smoke test through each real connector before a deploy.
- Serve demos and the sales deck without a client chatbot account.
- Be the agent the Slack transport wraps.

The harness always calls `get_context` first and `log_answer` last. It is the one place the full loop is enforced, which is why its numbers are an upper bound on what a BYO chatbot achieves. We report both when we have both.

## 10. Evaluation

### 10.1 Two sets per tenant

The trap set guards correctness. Hand-written items that exercise every declared trap, every refusal path, and every disclosure. Because `prefer` is the norm, each fake tenant keeps exactly one real `ask` so the ask path, the abandonment metric, and pins all have a live case.

The realistic set guards adoption. Items weighted toward what stakeholders actually ask, and its headline number is first-turn answer rate with correct disclosures. It includes an over-refusal class: answerable questions that look risky, so a prompt change that makes the model timid is caught. It is scored on two models per run, the default and a cheap one, so we can publish a floor and a recommended tier.

A golden item records the question, the expected status (`resolved`, `needs_clarification` with which trap, `unanswerable`, `invalid`), the expected spec, expected numbers, expected disclosures, and a `verified` flag saying a human checked the number against a known report.

### 10.2 Development on fake_companies

fake_companies gives four verticals (B2C SaaS, retail DTC, B2B services, CPG wholesale), each with a dbt project, MetricFlow metrics on DuckDB, deterministic data, labeled anomalies, and a corruption layer. Understory develops and tests against them and never against client data.

- Four tenants under `tenants/`, one per vertical. Any shared-core change must pass all four.
- Trap set numbers are exact because the data is seed-deterministic.
- The realistic set is partly generated from the seeded ground truth: people ask about a month when something happened in it, so questions are drawn from the labeled events and padded with questions that have no event behind them, then hand-edited to sound like a stakeholder. Real question logs from a client, with consent, and rephrased against synthetic entities, are the best source of wording.
- Corruption-fault tenants are in the eval matrix. A loading-lag fault should produce a freshness disclosure and a missing-partition fault a "we don't know". These are descriptive answers, not why answers, so they belong here and not in phase 3.

What fake data cannot test: a semantic layer built by someone else, legacy metric names, real phrasing, BigQuery compile latency, connector auth. Those are tested per client, by that client's golden sets.

### 10.3 A client's golden sets are a deliverable

Every engagement ships trap and realistic sets for the client, written with the data owner, as billable work. Sources, cheapest first:

1. Drafted from the catalog: one canonical question per metric and per metric-by-dimension, with expected numbers taken by running Understory once and a human approving the snapshot. These guard regression, marked `verified: false`.
2. Drafted from the traps registry: one item per `ask`, one disclosure check per `prefer`.
3. Real questions from a help channel export or a one-hour interview with the data owner.
4. Gaps promoted from the backlog (section 10.4).

A command drafts (1) and (2) from the tenant directory. A skill runs the (3) interview and turns the transcript into draft items. The data owner verifies the twenty numbers that matter.

### 10.4 Gap promotion

A command reads `mart_semantic_backlog` from the warehouse and drafts a golden item per gap into the tenant directory. The analyst builds the metric, fills in the expected answer, and the next deploy proves the gap is closed. The log is both roadmap and regression test, and the client sees their semantic layer's coverage grow week over week.

The server never reads its own gaps. The one place Understory reads its history is this command, run by an analyst against the warehouse.

### 10.5 Metrics

| Metric | Set | Source |
|---|---|---|
| First-turn answer rate with correct disclosures | Realistic | Headline adoption number |
| Coverage rate | Both | Share answered through `query_metrics` rather than `run_sql` or refusal |
| Resolution accuracy | Trap | Asked when the item says ask, and only then |
| Answer accuracy | Both | Numbers match within tolerance |
| Disclosure rate | Both | Required disclosures present in the logged draft |
| Over-refusal rate | Realistic | Answerable items refused |
| Abandonment rate | Production and harness | Asks returned with no resubmission. The trigger for roadmap 1.8 |
| Capture rate | Production | Share of sessions where `log_answer` was called, per tenant, per chatbot |
| Cost per question | Both | Tokens and dollars, per model |
| Backlog | Production | `mart_semantic_backlog`, gaps ranked by count and distinct users |

### 10.6 Improving prompt surfaces over time

Refusals from `get_context` alone are prompt engineering, and they are ongoing work. The context document, tool descriptions, and connector instructions are versioned and changed one at a time. Every change runs both sets in both modes before and after. The realistic set's over-refusal class is what tells us a change made the model timid; the trap set is what tells us it made the model reckless.

## 11. Telemetry events

| Event | Family | Fields beyond the common ones |
|---|---|---|
| `tool_called` | events | tool, status, latency_ms |
| `clarification_returned` | events | trap_id, options |
| `clarification_applied` | events | trap_id, choice |
| `query_executed` | events | metrics, dimensions, sql_hash, row_count, governed, cache_hit |
| `refused` | events | reason (`unanswerable`, `invalid`, `uncovered`, `sql_rejected`, `too_broad`, `prose`), phrase |
| `answer_logged` | events | numbers_checked, numbers_unsourced, disclosures_present |
| `gap_recorded` | gaps | question, spec_tried, missing (entity names or tables), reason, sql, tables_touched, nearest_matches |

Common fields on `events`: `event_id`, `tenant`, `user_hash`, `session_id`, `ts`. Text-bearing fields go to the `text` family under the same `event_id`. Gap records carry `event_id` and `ts` but no `user_hash`; distinct-user counts are joined from `events` in the warehouse.

## 12. Request flows

Covered question. "Net revenue by category over the last six months." The chatbot calls `query_metrics` with a spec naming `net_revenue`. The traps check finds no `revenue` phrase because the metric is named directly. Time anchors to the latest available date and is disclosed. One round trip.

Preferred reading. "How were sales in the West last quarter?" The traps check matches `sales` to the revenue collision, whose policy is `prefer net_revenue`, and `west` to the dimension-role rule, `prefer order__country`. Both are applied. The answer carries two disclosures, one of which says how to ask for gross revenue. One round trip, and the user learned a word.

Asked question. "What was our margin in Q2?" The margin collision is the tenant's one `ask`, with a stated reason. The response is `needs_clarification` with two options and hints. The user answers, the chatbot resubmits with a `clarifications` entry, the second call runs. If the user never resubmits, that is an abandonment.

Uncovered question. "What was our return rate for orders that used a promo code?" The spec is valid but the semantic layer has no promo dimension. `query_metrics` returns `invalid` naming the missing dimension and writes a gap. The chatbot falls to `run_sql` with the question and the reason "no promo_code dimension on orders". The answer discloses that it came from ad hoc SQL and why. The gap record now has the SQL, and `mart_semantic_backlog` shows the data team a draft of the metric to build.

Declared unanswerable. "What is our profit by SKU?" The traps check matches and returns `unanswerable` with the reason. The chatbot says so and offers category-grain margin. A gap is written so the count of people asking is visible.

## 13. Repository layout

```
understory/
  CONTEXT.md       glossary
  docs/adr/        decisions
  knowledge/       dated measurements and eval findings
  src/understory/
    server/        MCP server, tool handlers, conversation store
    catalog/       semantic_manifest.json reader, get_context assembly
    traps/         registry schema, matcher, compile and CI check
    semantic/      SemanticLayer protocol, metricflow_local, dbt_cloud
    warehouse/     Warehouse protocol, duckdb, bigquery
    guard/         run_sql parser and policy
    telemetry/     Parquet append writer, three families, event schemas
    harness/       agent, eval runner, BYO mode, golden drafting, gap promotion
  dbt_understory/  dbt package: sources over the log prefix, staging, marts
  tenants/         four fake_companies tenants, as fixtures
  tests/
  docs/
```

Python 3.13, uv, Pydantic v2, the official `mcp` SDK, sqlglot, pyarrow, DuckDB, `google-cloud-bigquery`, `dbt-metricflow` in the semantic layer process only.

## 14. Build sequence

Draft 0.2's steps 1 through 6 are built: catalog and discovery, governed query, traps, escape hatch, telemetry and `log_answer`, harness and evals. What follows is the next phase, each step demonstrable, all before the first pilot deployment.

1. Harness efficiency. Prompt caching, message history across the clarification turn, usage and cost capture, and budget guards are merged. Payload trimming and bounded eval concurrency remain (roadmap 1.7).
2. Policy flip. `prefer` as the norm, `why` required on `ask` and enforced by the registry check, default window as a preferred tenant setting, the four fake tenants and their trap sets rewritten to match.
3. Gaps. Required `question` and `reason` on `run_sql`, the fallback disclosure, gap records keyed on what was missing, the `gaps` family, `mart_semantic_backlog` over it, abandonment in the eval report.
4. Realistic set. Generator from seeded ground truth with hand-edited wording, corruption-fault tenants in the matrix, over-refusal class, BYO mode, two-model scoring.
5. Golden authoring. Draft command from catalog and traps, the data-owner interview skill, snapshot approval with the `verified` flag.
6. External tenant mount. Read a tenant directory from a client repository, docs and README for that layout.
7. Gap promotion command.
8. Real users. Friendly stakeholders on a fake tenant through Claude.ai over a tunnel, then Northern Nights on their own data, then a larger company.

## 15. Open questions

- Where the gentle guidance lives: the hint strings on traps, the `prefer` disclosures, or both. Decide after the first friendly users have seen answers.
- Exact connector auth options on ChatGPT Enterprise and Claude.ai today, and whether either accepts a static bearer token for a pilot.
- MetricFlow compile latency on BigQuery-backed projects, and whether the compile cache is enough or a warm process is needed.
- Whether `run_sql` should be visible to every user or gated by a tenant-level role list. The MVP exposes it to everyone with the ungoverned disclosure and we measure how often it is used.
- Retention default for the `text` family, and whether some clients want zero text retention.
- How much of `get_context` can be generated from dbt docs versus written by hand.
- Whether the missing-entity key for gaps splits or lumps well enough in practice, before clustering is built.
