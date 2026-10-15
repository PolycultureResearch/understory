# The follow-up question, the answer check, and the event loop

2026-10-12. Items 1 to 3 of the fix list in `grill-2026-10-05.md`.

## What changed

**The user's words (item 1).** `MetricSpec.question` is required on the wire:
the published schema marks it required and `query_metrics` refuses a spec
without it before anything runs, with no gap record. A new optional field,
`earlier`, carries the user's previous messages the latest one builds on. The
traps matcher reads `earlier` and `question` as one text, which is what the
oracle replay already did with the golden item's turns. The field descriptions,
the `query_metrics` tool description and the harness docstring all say why: a
follow-up like "split that by country" needs the message that said what "that"
is. `question` stays optional in Python, where the semantic layer and tests
build specs with no user behind them.

**`log_answer` (item 2).**

- A number no result holds passes when it is a difference, sum, ratio or
  percent change of two numbers the draft sources, a column total, or a share
  of one. The operands have to be in the draft. Pairing every cell of a large
  result would let almost any number through.
- A draft owes the disclosures of the results its numbers came from, newest
  result first when two hold the same value. A draft quoting no number owes
  those of results no passing draft has covered. `Session.disclosures`, which
  only grew, is gone; each stored result keeps its own.
- Status has a third value, `missing_disclosures`. `unsourced_numbers` wins
  when both apply. The harness's structural check hands a reply back for a
  missing disclosure once per turn, since presence is a 60% word match and a
  false miss should not use up the retries.
- Harness rule 8 now allows the same arithmetic the check does.

**Threads (item 3).** Each MCP tool body runs on a worker pool of 16, with the
request's context copied in. The session is found on the loop first. `Session`
has a lock around `remember` and the review. The pool is our own because
`asyncio.to_thread`'s default is sized to the CPU count, five threads on one
vCPU.

## What the runs said

Alpenglow, multi-turn set, Sonnet, five repeats, 50 item runs per cell,
`checks.spec_disclosures` off. Before is the rule-off column of
`grill-2026-10-05.md`, run 2026-10-07.

| measure | harness before | harness after | BYO before | BYO after |
|---|---|---|---|---|
| passed | 47/50 | 49/50 | 43/50 | 46/50 |
| material bypasses | 10/49 | 3/50 | 21/50 | 1/50 |
| first-turn answer rate | 84% | 88% | 92% | 84% |
| extra queries per item | 0.33 | 0.08 | 0.56 | 0.10 |
| cents per item | 3.59 | 3.35 | 4.27 | 3.61 |

- BYO bypass went from 42% to 2%. The four that remain across both modes are
  `returns_context_then_month` (three) and `board_deck_revenue_then_number`
  (one), both items whose first turn runs no query.
- The five failures are not bypasses: four replies left a disclosure out
  (three in BYO, where nothing hands the reply back) and one BYO run never
  called `log_answer`.
- BYO first-turn fell eight points. Not examined. The carry-over kind is at
  65%, and five of its twenty runs are the margin ask, which is meant to stop.

## Limits

- One pass, one model, one tenant, ten items. Haiku was not run, and it was
  the weaker courier in every earlier cell.
- All three changes landed together. The bypass count can only move with item
  1, but pass rate, cost and first-turn also carry the new check and prompt
  rule.
- The trap and realistic sets were not run live after the change. They pass
  deterministically on all four tenants.
- The arithmetic check has unit tests only; no live item asks for a change
  between two periods.
