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
The policy that stops and returns a short ranked set of options with hints. Reserved for traps where the wrong reading is materially misleading.

**Prefer**:
The policy that applies a declared candidate and adds a disclosure. The default balance between rigor and flexibility: "revenue" resolves to net revenue, and the answer says so.

**Disclosure**:
A sentence the reader of an answer must see about what was applied to get the number: which candidate a trap chose, which time window, which dimension role.
_Avoid_: caveat, note, warning
