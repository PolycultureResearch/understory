# Understory

The natural-language interface to a client's analytics. Employees ask questions in the chatbot they already use; Understory exposes the client's semantic layer as tools, asks or refuses when a confident answer would be a plausible wrong number, discloses what it applied, and logs the gaps so the data team can close them.

## Language

### Resolving a question

**Trap**:
A declared point where a confident mapping of a phrase would be the wrong outcome. Traps live in the tenant's traps registry, never in the semantic layer.
_Avoid_: annotation, rule, override

**Policy**:
What a trap does when it fires. One of `ask`, `prefer`, or `disclose`.

**Ask**:
The policy that stops and returns a short ranked set of options with hints. The exception, not the norm: every ask carries a stated reason, and the client's data owner signs off on the list.

**Prefer**:
The policy that applies a declared candidate and adds a disclosure. The norm: "revenue" resolves to net revenue, and the answer says so.

**Default window**:
The time window applied when a question names none. Preferred and disclosed, never asked. Each tenant sets its own; trailing 30 days when unset.

**Disclosure**:
A sentence the reader of an answer must see about what was applied to get the number: which candidate a trap chose, which time window, which dimension role, or why the answer fell back to ad hoc SQL. Human-facing, and derived from provenance wherever possible.
_Avoid_: caveat, note, warning

**Provenance**:
The machine-readable record of where a number came from: the exact tables and columns touched, the method that built the SQL, and whether it was governed. For the log and for audit, not for the reader.
_Avoid_: lineage, source, citation

**Governed**:
A property of a result: the number came through the semantic layer. An ungoverned result came from ad hoc SQL and says so.
_Avoid_: verified, trusted, official

**Covered**:
A property of a question relative to the semantic layer: the metrics and dimensions it needs exist. A covered question can still be answered ungoverned if the chatbot skips the governed path.
_Avoid_: supported, in scope

**Clarification**:
An ask that reached the user, and the user's choice when it comes back. Not a refusal.

**Refusal**:
An answer with no number and a stated reason. The reason names who decided: the server (unanswerable, SQL rejected, too broad), a catalog gap (invalid, uncovered), or the model itself (prose refusal). A prefer with a disclosure is not a refusal.
_Avoid_: error, decline

### The flywheel

**Gap**:
A question the semantic layer could not answer as governed, recorded with what was tried, what was missing, why the answer fell back, and the ad hoc SQL that answered it instead. The unit of the data team's backlog. De-identified by construction so the people who fix it can read it.
_Avoid_: refusal (a refusal is one outcome; a gap is the record that accumulates), miss, failure

**Abandonment**:
An ask that was returned and never resubmitted with a choice. The signal that clarification has become onerous.

### People and deployments

**Client**:
The company in the commercial engagement. Owns its dbt project, its semantic layer, and its tenant directory.

**Tenant**:
One deployed configuration and its running container: the context document, traps registry, semantic manifest, and golden sets. One per client today, but the two are not the same thing.
_Avoid_: customer, account, deployment (a tenant is what gets deployed, not the act)

**Data owner**:
The named person at the client who signs off on which schemas are in scope, which traps ask, and who may read question text.

**Session**:
A run of questions from one user, reconstructed for analysis from timestamps and the hashed user identity.
_Avoid_: conversation (that is the in-memory thing below)

**Conversation**:
The short-lived in-memory record the server keeps per chatbot connection so the number check can see earlier results. Never persisted.
_Avoid_: session

### The reference chatbot

**Agent**:
The model loop that drives the tools: the same tools the chatbot sees, in process, behind a pinned model.

**Harness**:
The agent plus the eval runner and command line. The reference chatbot, always cheaper and stricter than a client's.
_Avoid_: bot, test rig

**Transport**:
The surface a person types into when Understory runs the agent itself. Slack is the first.
_Avoid_: integration, channel

### Evaluation

**Trap set**:
The hand-written golden questions per tenant that exercise every declared trap and refusal path. Guards correctness.
_Avoid_: golden set (ambiguous now that there are two)

**Realistic set**:
Golden questions per tenant weighted toward what stakeholders actually ask, partly generated from seeded ground truth. Its headline number is first-turn answer rate with correct disclosures. Guards adoption. Each item has a kind: `event` (a month the ground truth put something in), `quiet` (a month with nothing), `over_refusal` (answerable but looks risky), later `fault`.
_Avoid_: adoption set, generated set

**Snapshot**:
Expected numbers taken by running Understory once, recorded with `verified: false`. Guards regression, not truth; a human sets `verified` after checking against a known report.
_Avoid_: golden numbers (they are not gold until verified)
