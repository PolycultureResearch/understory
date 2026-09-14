---
status: accepted
date: 2026-09-12
---

# A tenant directory lives in the client's dbt repository

The context document, traps registry, semantic manifest, and golden sets that make up a tenant are the client's context layer, and owning it is part of what they buy. They live next to the client's dbt project in the client's repository, so a change to a metric, its trap, and its golden question is one reviewable pull request. The Understory container mounts the directory and reads it; it never owns it. The four fake_companies tenants stay in this repository as fixtures only.

## Considered options

Keeping tenants in the Understory repository was simpler for the MVP and is what the scaffold did. It would have made Polyculture the owner of every client's context and put client-specific content in a repository the client does not control.
