# Understory MVP: design

Polyculture Research | Draft 0.2 | September 10, 2026

Supersedes sections 3 through 8 of `agentic-analytics-architecture.md` (draft 0.1) for the MVP. The roadmap in `understory-roadmap.md` holds everything from 0.1 that is deferred.

## 1. What Understory is

Understory is the natural-language interface to a client's analytics. The client's employees ask questions in the chatbot they already use (ChatGPT Enterprise, Claude, or later a Slack bot). Understory is the MCP server that chatbot talks to. It exposes the client's dbt semantic layer as tools, refuses or asks when a question is ambiguous, shows where every number came from, and logs the trace so the semantic layer improves over time.

The billable work at each client is the dbt data model and semantic layer. Understory is what makes that work usable through chat within days of finishing it, at a price the chatbot subscription mostly absorbs.

Three constraints shape everything below.

The chatbot is the client's. We do not control its prompt, its model, or whether it calls our tools in the order we would like. Anything that matters is enforced inside a tool call or not at all.

The semantic layer is the source of truth for every number. Understory never changes a number. It maps words to entities, decides when to ask, and discloses what it applied.

Boring tools. Python, DuckDB, Parquet, dbt, MetricFlow. Nothing that needs a platform team to run.

## 2. Principles carried from draft 0.1

- Deterministic components decide. The LLM parses and renders.
- Refuse or ask rather than return a plausible wrong number.
- Surface ambiguity as a short ranked set of choices with reasons.
- Ambiguity is declared in config and checked in CI, not inferred at runtime.
- Every refusal, clarification, and SQL fallback is backlog for the semantic layer.
- Shared core, bespoke content.

One principle is new. Understory is write-only on its own history. The server appends to the question log and cannot read it. Section 8 explains why.

## 3. System overview

```
  ChatGPT Enterprise / Claude.ai            Understory harness (Python, OpenRouter)
           │  MCP over HTTPS                        │  same tools, in-process
           ▼                                        ▼
 ┌────────────────────── Understory server (one container per client) ──────────────────────┐
 │                                                                                          │
 │  get_context ─────────┐                                                                  │
 │  list_metrics ────────┤                                                                  │
 │  describe_metric ─────┼──► Catalog  ◄── semantic_manifest.json (dbt parse)              │
 │  search_dimension_values ┘                                                               │
 │                                                                                          │
 │  query_metrics ──► Traps check ──► SemanticLayer.compile(spec) ──► Warehouse.run(sql)    │
 │        │               │                (MetricFlow, local or Cloud)   (DuckDB, BigQuery) │
 │        │          needs_clarification                                                    │
 │        │          unanswerable / uncovered                                               │
 │                                                                                          │
 │  run_sql ─────────► SQL guard ──────────────────────────────────► Warehouse.run(sql)     │
 │                     (read-only, row cap, timeout, flagged provenance)                    │
 │                                                                                          │
 │  log_answer ──────► Number check (every number in draft traces to a result this session) │
 │                                                                                          │
 │  Telemetry writer ──► append-only Parquet ──► object store / log dir  ──► dbt package    │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

The harness on the right is ours. It runs the same tools in-process with a pinned model through OpenRouter. It exists so we can run evals without depending on a client's chatbot, demo without a client account, and later become the Slack bot. It is part of the MVP because the eval loop is not optional.

## 4. Tools

Seven tools. Each returns structured JSON with a `provenance` block and, where relevant, `required_disclosures`.

| Tool | Purpose | Returns |
|---|---|---|
| `get_context` | The client's business semantics in a few kilobytes | Markdown: what the company does, metric conventions, fiscal calendar, known gotchas, current data freshness |
| `list_metrics` | Discovery | Name, label, description, type, available dimensions, synonyms |
| `describe_metric` | One metric in depth | Definition, expression, filters baked in, upstream models, freshness, example questions |
| `search_dimension_values` | Resolve "the West" to real values | Matching values for a dimension, with counts |
| `query_metrics` | Execute a metric spec | Rows, compiled SQL hash, applied conventions, disclosures. Or `needs_clarification`, `unanswerable`, `invalid` |
| `run_sql` | Escape hatch | Rows with a `governed: false` flag and the SQL that ran |
| `log_answer` | Capture the draft answer, check numbers | `pass` or a list of numbers with no source |

### 4.1 `get_context`

The tool that moves accuracy most and costs least to build. The Cube benchmark cited in draft 0.1 found a 4 KB context document moved accuracy more than model choice did. The document is a Markdown file per client, written by us during the engagement, plus a generated tail. The generated tail lists the metrics by category, the fiscal calendar if any, and the data freshness computed at request time.

Chatbots do not reliably call a context tool first. The harness always does. For BYO chatbots we also ship the same content as connector instructions where the platform allows it, and `list_metrics` repeats the conventions section in its response so a chatbot that skips `get_context` still sees it.

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

1. Validate the spec against the catalog. Unknown metric or dimension returns `invalid` with the nearest matches.
2. Run the traps check (section 5) against both the spec and the question text. A trap with policy `ask` that is not satisfied by a `clarifications` entry returns `needs_clarification` with options. A `disclose` trap adds to `required_disclosures`. An `unanswerable` match returns that status with the declared reason.
3. Resolve time. `end` defaults to the metric's latest available date, never today. The applied window is always disclosed.
4. Compile through the semantic layer, execute through the warehouse adapter, cap rows.
5. Return rows plus provenance and disclosures. Log the event.

There is no separate resolve step and no signed token. The traps check is deterministic and cheap, so running it inside `query_metrics` costs nothing and removes two round trips and a session-state mechanism. Draft 0.1's token enforced tool ordering, which is not the same as user involvement, so it bought less than it cost.

### 4.3 `run_sql`

The semantic layer will be young at every new client. SQL will carry a lot of early questions, and that is fine as long as the answer says so. `run_sql` is a first-class path with its own guard.

- Read-only. The guard parses with sqlglot and rejects anything other than a single SELECT.
- Row cap and statement timeout from tenant config.
- Schema scope from tenant config. Only the mart and semantic schemas, never raw or staging by default.
- Response carries `governed: false`, the SQL that ran, and the tables it touched.
- Every call logs the question, so the weekly review sees what the semantic layer could not answer.

`describe_table` and `list_tables` are not separate tools. `get_context` names the tables `run_sql` may touch and `run_sql` accepts `information_schema` queries.

### 4.4 `log_answer`

The chatbot sends its draft answer before showing it. The server extracts numbers from the draft and checks each against the results returned earlier in the session, with rounding tolerance. It returns `pass` or the list of numbers with no source. It also captures the draft for the log.

This tool is advisory in a BYO chatbot. Nothing forces the model to call it. We measure the call rate per tenant as an eval metric and accept that capture is partial. The harness always calls it. There is no LLM critic in the MVP.

## 5. Traps registry

This replaces the annotation schema from draft 0.1. The concern with that schema was right. Per-metric blocks that carry defaults, filters, and calendars are a second place to define a metric, and two places drift.

The split is simple. Anything that changes a number belongs in the semantic layer. Anything about how to resolve a question belongs in the traps registry.

| Belongs in dbt / MetricFlow | Belongs in `traps.yml` |
|---|---|
| Metric definitions, filters, measures | Which phrases collide, and what to do about it |
| Descriptions and labels | Which concepts are declared out of scope, and why |
| Synonyms (in `meta` until OSI lands) | Which dimension roles collide |
| Fiscal calendar as time dimensions | Whether to ask or disclose a convention |
| Currency, cancelled-order handling | Priority order when several traps fire |

A registry for a retail client looks like this. The example uses metrics from the `retail_dtc` vertical in fake_companies, where `gross_revenue` and `net_revenue` both exist.

```yaml
version: 1

collisions:
  - phrase: [revenue, sales, top line]
    candidates: [net_revenue, gross_revenue]
    policy: ask                       # or: prefer net_revenue
    hint:
      net_revenue: "After discounts and refunds. Used in board reporting."
      gross_revenue: "Before discounts and refunds."

  - phrase: [margin]
    candidates: [gross_margin, margin_rate]
    policy: ask

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
    policy: ask_if_absent

unanswerable:
  - phrase: [profit by sku, sku margin]
    reason: "COGS is only available at category grain."
  - phrase: [ltv, lifetime value]
    reason: "No LTV model yet. Ask about repeat order share instead."
```

Three policies. `ask` returns options. `prefer X` applies X and adds a disclosure. `disclose` applies whatever the spec said and adds a disclosure. There is no silent `apply`, because the number-changing defaults that needed it now live in the metric.

Disclosures derive from what was resolved where possible. If the resolved time dimension is a fiscal quarter, the disclosure says so from the dimension's own description. The `disclose` strings in the registry are for conventions that have no entity to hang off.

Matching is a normalized phrase match over the question text and the spec. No lemmatized n-gram index. The chatbot's model is already good at mapping "revenue" to a metric when the descriptions are good. The registry exists to catch the handful of declared traps where a confident mapping is the wrong outcome, and a dozen entries covers that at a typical client.

The compile step runs in CI. It reads `semantic_manifest.json` and `traps.yml`, checks every candidate exists, and detects synonyms declared in `meta` that map to more than one metric. An unadjudicated collision fails the build.

## 6. Semantic layer and warehouse

### 6.1 The lock-in question

dbt's own MCP server is not the answer. Its semantic layer tools all need dbt Cloud, and the tool set is discovery plus `query_metrics` with none of the gating above. Understory needs its own server regardless. The question is only what compiles a metric spec to SQL.

The MVP takes open-source MetricFlow as the default and puts it behind an interface so dbt Cloud is a config switch for clients who already pay for it.

```python
class SemanticLayer(Protocol):
    def catalog(self) -> Catalog: ...                     # from semantic_manifest.json
    def compile(self, spec: MetricSpec) -> CompiledQuery: ...   # SQL + hash
    def dimension_values(self, dim: str, q: str) -> list[Value]: ...

class Warehouse(Protocol):
    def run(self, sql: str, *, timeout: int, row_cap: int) -> Result: ...
    def latest_date(self, relation: str, column: str) -> date: ...
```

Two `SemanticLayer` implementations.

`MetricFlowLocal` runs `mf query --explain` against the client's dbt project to get SQL and hands that SQL to the `Warehouse` adapter. fake_companies and Breakdown's `local` provider both use this path today. It needs dbt-core and the warehouse adapter in the container and Python 3.13 or lower, which is why the semantic layer runs in its own process from the MCP server. Latency is a few seconds per compile. Compiled SQL is cached by spec hash, so repeated questions do not pay it twice.

`DbtCloud` calls the Semantic Layer API through `dbt-sl-sdk`. Same interface, no local dbt install. Use it when a client already has dbt Cloud.

Executing the SQL ourselves rather than letting MetricFlow execute it matters. It gives one connection, one provenance format, and one row cap for both the governed path and `run_sql`.

Breakdown's manifest-to-SQL bridge is deliberately narrow (one measure, one grain, one dimension) and is not a general compiler. It is not used here. If OSI or a maintained open-source compiler matures, it slots in as a third implementation.

### 6.2 Warehouse adapters

Two in the MVP. `DuckDB` for development, tests, and the fake_companies tenants. `BigQuery` because that is where the first client is. Both use a single service account or local file. Per-user warehouse identity is out of scope, see section 8.

Parquet is the interchange format for everything Understory writes: telemetry, cached results, eval fixtures. DuckDB reads it directly, BigQuery loads or reads it externally.

## 7. Tenants and hosting

One container image, one running container per client, configuration mounted. Multi-tenancy inside a process is a source of bugs we do not need to invite for a handful of clients.

A tenant is a directory.

```
tenants/alpenglow/
  tenant.yml            # warehouse connection, semantic layer mode, row caps, schema scope
  context.md            # the get_context document
  traps.yml             # section 5
  semantic_manifest.json  # copied in by the dbt deploy, or a path to the dbt project
  golden/questions.yml  # section 10
```

Hosting is Polyculture-run for the MVP, one small container per client on Cloud Run or a VM, reachable over HTTPS. Clients who require in-cloud deployment get the same image. Nothing in the design depends on where it runs.

### 7.1 Connector authentication

ChatGPT Enterprise and Claude.ai both add custom connectors as remote MCP servers over HTTPS. Both expect OAuth 2.1 at the connector level. This is authentication of the chatbot to Understory and is separate from warehouse identity. We cannot skip it, but we can keep it thin.

The MCP server validates tokens issued by the client's identity provider (Google Workspace or Microsoft Entra at most clients). Understory never stores passwords and never maps a user to warehouse credentials. The only thing it takes from the token is a stable subject identifier, which section 8 hashes before it reaches any log.

Verify the exact connector auth options against the ChatGPT Enterprise admin docs before the first deployment. They have changed several times this year.

## 8. Identity, logging, and privacy

### 8.1 Single service account

The server connects to the warehouse with one read-only service account per tenant, scoped to the mart and semantic schemas. Per-user OAuth passthrough is dropped from the MVP. Most employees at a client have no warehouse account, and maintaining the mapping is a support burden that does not serve the first clients.

The consequence is that row-level security cannot be enforced by the warehouse. Every user of the connector sees what the service account sees. Tenant config states this and the client signs off on which schemas are in scope.

### 8.2 Who asked what

Every event carries `user_hash`, an HMAC of the subject identifier from the connector token keyed with a per-tenant secret. The secret lives in the server's secret store and never in the warehouse. A reader of the log sees that the same person asked twelve questions, not who they are. If the client needs to re-identify a user for a support case, we run the HMAC over their directory and match, with their data owner in the loop.

### 8.3 Write-only

Question text is sensitive. What people ask reveals what they are worried about, and a question history table is the kind of thing that ends up in a screenshot. The server therefore has no read access to its own history, and the log is split so most readers never see text.

The telemetry writer appends Parquet files to a log prefix. Locally that is a directory. In BigQuery deployments it is a GCS prefix where the server's log identity holds `objectCreator` only. The server cannot list or read what it wrote. The warehouse reads the prefix through an external table or a scheduled load, and the dbt package models it from there.

Two event families, two prefixes, two access levels.

| Prefix | Contains | Readers |
|---|---|---|
| `log/events/` | Timestamps, `user_hash`, tool name, status, metric and dimension IDs, trap IDs, row counts, SQL hash, latency | The client's analytics team and Polyculture |
| `log/text/` | Question text, spec, compiled SQL, draft answer, keyed by `event_id` | Polyculture and the client's named data owner |

The dbt package builds `fct_questions`, `fct_clarifications`, and the eval marts from `events` alone. Marts over `text` exist for the weekly backlog review and live in a restricted schema. Retention on `text` is a tenant setting, default 90 days.

Two service accounts per tenant. The query identity reads marts and cannot touch the log prefix. The log identity creates objects under the log prefix and can read nothing.

### 8.4 Sessions

The server keeps a small in-memory session per MCP connection so `log_answer` can check numbers against earlier results. Sessions expire after inactivity and nothing in them is persisted beyond the events already logged. Sessionization for analysis is done in dbt from timestamps and `user_hash`.

## 9. The harness

A Python agent that talks to the same tools in-process, with the LLM behind OpenRouter. Pydantic AI with the OpenRouter provider, the same pattern Observation Deck uses. Model is a config value and evals pin it. The default is `anthropic/claude-sonnet-5` through OpenRouter, chosen for cost per eval run rather than peak accuracy, so that a model that does well on our evals is a floor for the client's chatbot rather than a ceiling.

The harness has three jobs in the MVP.

- Run the golden set on every dbt deploy and weekly. Record resolution status, numbers, disclosures, and `log_answer` result per question.
- Serve demos and the sales deck without a client chatbot account.
- Be the shape the Slack bot fills in later. The Slack transport is a thin adapter over the same agent.

The harness always calls `get_context` first and `log_answer` last. It is the one place the full loop is enforced, which is why the eval numbers it produces are an upper bound on what a BYO chatbot achieves. We report both when we have both.

## 10. Development on fake_companies

fake_companies gives four verticals (B2C SaaS, retail DTC, B2B services, CPG wholesale), each with a dbt project, MetricFlow metrics on DuckDB, deterministic data, and labeled anomalies. Understory develops and tests against them and never against client data.

How each part of fake_companies is used.

- Four tenants under `tenants/`, one per vertical, generated from the existing configs. The retail tenant carries the `revenue` collision. The B2C tenant carries an `mrr` versus `new_mrr` collision. Any shared-core change must pass all four, which is what keeps the core vertical-agnostic.
- Golden question sets per tenant, written by hand against the metrics each vertical exposes. Expected answers are exact because the data is seed-deterministic. A golden item records the question, the expected status (`resolved`, `needs_clarification` with which trap, `unanswerable`, `invalid`), the expected spec, and the expected numbers.
- The corruption layer feeds refusal tests. A tenant generated with a loading-lag fault should produce a freshness disclosure, and a missing-partition fault should show up as a "we don't know" rather than a low number.
- `ground_truth.json` is held for the explanation engine in a later phase. The MVP does not answer why questions.

CI runs the golden sets for all four tenants through the harness against DuckDB on every push. That is the whole test of the shared core.

## 11. Telemetry events

| Event | Fields beyond the common ones |
|---|---|
| `tool_called` | tool, status, latency_ms |
| `clarification_returned` | trap_id, options |
| `clarification_applied` | trap_id, choice |
| `query_executed` | metrics, dimensions, sql_hash, row_count, governed, cache_hit |
| `refused` | reason (`unanswerable`, `invalid`, `uncovered`, `sql_rejected`), phrase |
| `answer_logged` | numbers_checked, numbers_unsourced, disclosures_present |

Common fields: `event_id`, `tenant`, `user_hash`, `session_id`, `ts`. Text-bearing fields go to the `text` prefix under the same `event_id`.

The dbt package models these into `fct_questions`, `fct_clarifications`, `fct_refusals`, and `mart_semantic_backlog`, which ranks phrases that refused or fell to SQL by frequency.

## 12. Eval metrics

| Metric | Source |
|---|---|
| Coverage rate | Share of golden questions answered through `query_metrics` rather than `run_sql` or refusal |
| Resolution accuracy | Asked when the golden item says ask, and only then |
| Answer accuracy | Numbers match the golden item within tolerance |
| Disclosure rate | Required disclosures present in the logged draft |
| Capture rate | Share of sessions where `log_answer` was called, per tenant, per chatbot |
| Backlog | Top refused and SQL-answered phrases from production logs |

## 13. Request flows

Covered question. "Net revenue by category over the last six months." The chatbot calls `query_metrics` with a spec naming `net_revenue`. The traps check finds no `revenue` phrase because the metric is named directly. Time anchors to the latest available date and is disclosed. One round trip.

Ambiguous question. "How were sales in the West last quarter?" The chatbot drafts a spec. The traps check matches `sales` to the revenue collision and `west` to the dimension-role rule. Revenue is `ask`, so the response is `needs_clarification` with two options and hints. The country role is `prefer order__country`, so it is applied and disclosed. The chatbot presents the choice, the user answers, the chatbot resubmits the spec with a `clarifications` entry. The second call resolves and runs.

Uncovered question. "What was our return rate for orders that used a promo code?" The spec is valid but the semantic layer has no promo dimension. `query_metrics` returns `invalid` naming the missing dimension and the nearest ones. The chatbot falls to `run_sql`, the answer is labeled ungoverned, and the phrase lands in `mart_semantic_backlog`.

Declared unanswerable. "What is our profit by SKU?" The traps check matches and returns `unanswerable` with the reason. The chatbot says so and offers category-grain margin.

## 14. Repository layout

```
understory/
  src/understory/
    server/        MCP server, tool handlers, session store
    catalog/       semantic_manifest.json reader, get_context assembly
    traps/         registry schema, matcher, compile and CI check
    semantic/      SemanticLayer protocol, metricflow_local, dbt_cloud
    warehouse/     Warehouse protocol, duckdb, bigquery
    guard/         run_sql parser and policy
    telemetry/     Parquet append writer, event schemas
    harness/       Pydantic AI agent over OpenRouter, eval runner
  dbt_understory/  dbt package: sources over the log prefix, staging, marts
  tenants/         one directory per tenant, four fake_companies tenants committed
  tests/
  docs/
```

Python 3.13, uv, Pydantic v2, the official `mcp` SDK (same as Breakdown), sqlglot for the SQL guard, pyarrow for Parquet, DuckDB, `google-cloud-bigquery`, `dbt-metricflow` in the semantic layer process only.

## 15. Build sequence

Each step ends with something demonstrable.

1. Catalog and discovery. Read `semantic_manifest.json` from the retail tenant, serve `get_context`, `list_metrics`, `describe_metric`, `search_dimension_values` over DuckDB. Demo in Claude.ai against the local server through a tunnel.
2. Governed query. `MetricFlowLocal` compile, DuckDB execute, `query_metrics` with validation and time anchoring. Provenance in every response.
3. Traps. Registry schema, matcher inside `query_metrics`, clarification round trip, CI compile check. The revenue collision demo.
4. Escape hatch. `run_sql` with the sqlglot guard and ungoverned labeling.
5. Telemetry and `log_answer`. Parquet writer, two prefixes, session number check, dbt package with `fct_questions` and `mart_semantic_backlog`.
6. Harness and evals. Pydantic AI over OpenRouter, golden sets for all four tenants, CI run.
7. BigQuery adapter, connector auth against a client IdP, container image, first deployment.

Steps 1 through 6 run entirely on fake_companies. Step 7 is the first time client infrastructure is touched.

## 16. Open questions

- A capable model refuses from `get_context` alone. In the first live eval, every unanswerable and invalid question was refused correctly without a single call to `query_metrics`, so the refusal never reached the traps registry or the telemetry. The answers were right and the backlog was empty. The harness now asks the model to call `log_answer` even when it never queried, which records the draft. Whether to add a dedicated refusal event, or to move the unanswerable list out of the context document so the model has to ask, is a decision for after the first client.
- Exact connector auth options on ChatGPT Enterprise and Claude.ai today, and whether either accepts a static bearer token for a pilot.
- MetricFlow compile latency on BigQuery-backed projects, and whether the compile cache is enough or a warm process is needed.
- Whether `run_sql` should be visible to every user or gated by a tenant-level role list. The MVP exposes it to everyone with the ungoverned label and we measure how often it is used.
- Retention default for the `text` prefix, and whether some clients want zero text retention with only the events family.
- How much of `get_context` can be generated from dbt docs versus written by hand. The first two clients will tell us.
