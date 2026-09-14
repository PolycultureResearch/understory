---
status: accepted
date: 2026-09-12
---

# Gaps are a third, de-identified log family

The flywheel promise is that questions the semantic layer cannot answer become the data team's backlog. That needs question text and the ad hoc SQL that answered instead, which the write-only telemetry design keeps in a restricted `text` family readable only by Polyculture and the data owner. Rather than widen `text` access, the server writes gap records to their own `gaps` family with no user identity, so the data team can read them. Gap records may carry SQL literals; the SQL only reaches mart schemas those readers already have.

## Considered options

Giving the data team access to `text` would expose who-asked-what patterns the design set out to protect. Having Polyculture curate a weekly backlog by hand does not scale and hides the flywheel from the people who turn it.
