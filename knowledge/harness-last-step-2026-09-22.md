# The last step: enforcing the closing check instead of asking for it

2026-09-22. What the first live run said about the harness prompt, what
changed because of it, and the ablation that measured the change.

## What the run said

Sonnet's four misses under the harness prompt and its one miss under the
connector instructions were nested: every item that passed with the harness
prompt passed in BYO mode, and three more passed in BYO mode only. All three
were the closing step. Two long answers (a "why", a "did it dip") ended
without `log_answer` being called; one reply opened by narrating the check.
Nineteen of the twenty-three items behaved identically in all four cells, and
every quiet and over-refusal item passed everywhere. The spread of the whole
experiment lived in five event items, and `sales_by_month_2025_q1` flipped
toward BYO on both models.

The token gap was the same story from the other side. Input tokens were equal.
Output was 78% higher in BYO mode because the reply was longer (no "be brief")
and `log_answer` was called 1.57 times per item rather than exactly once, and
every call carries the whole draft. The harness saved a quarter of the cost
partly by skipping the check it exists to enforce.

## What changed

1. **The check is structural.** An output validator on the harness agent runs
   the final reply through `log_answer` when the model did not, records it in
   the trace as a structural call, and sends an unsourced review back to the
   model for one more turn. Capture is 100% by construction wherever we control
   the loop, which includes the Slack transport. BYO mode leaves it off, since
   a connector has no such hook. The prompt rules that asked for the check,
   and rule 10 that named the thing it forbade, are gone from the lean prompt.
2. **`log_answer` says what the next message is.** The review now carries a
   `how_to_read`: on a pass, the draft addressed to the user; on unsourced
   numbers, rewrite so every number traces to a result and send that as the
   next message, the check staying between us. Positive wording, because a
   model told what not to say tends to say it. This reaches every chatbot, not
   only the harness, and narration appeared in both modes.
3. **The prompt is a variable.** `eval --prompt harness|lean`. `lean` is the
   connector instructions plus the two lines with evidence behind them: call
   `get_context` first, and use the discovery tools when unsure. No brevity
   rule; it cost a disclosure and two checks.
4. **Measuring on a handful.** `eval --id ... --repeat N` runs the items that
   carry the variance several times and reports pass counts per item, so a
   fix reads as 1/5 becoming 5/5 rather than as one coin flip.

## The ablation

Five items, five repeats each, Sonnet, four variants. The five are the event
items that moved between cells on the first run, so they are biased toward
the hard end and will regress toward passing on repeat whatever we do; the
question is which variant moves them most, and the change is confirmed on
the full set afterwards.

| variant | passed | first-turn | capture | cents/item |
|---|---|---|---|---|
| A. harness prompt, no check | 15/25 | 88% | 72% | 3.00 |
| B. harness prompt + structural check | 25/25 | 100% | 100% | 2.96 |
| C. lean prompt + structural check | 24/25 | 96% | 100% | 5.80 |
| D. BYO (connector instructions, no check) | 24/25 | 96% | 100% | 5.60 |

Per item, A lost `refunds_why` four times in five and `sales_by_month` three
times in five, every one of them "log_answer never called", plus two dropped
disclosures on `returns_by_quarter` and one narrated check on `top_line`. B
lost nothing. C lost one `refunds_why` run to a number left out of the reply;
D lost one `returns_by_quarter` run to the dropped "refunds" alternative.

Three conclusions.

- **The structural check is the whole fix.** It turned the two chronic items
  from 1/5 and 2/5 into 5/5 and cost nothing: B is the same price per item
  as A. It was never a prompt problem in the sense of wording; it was a step
  the model could skip, and now it cannot.
- **The harness prompt is worth keeping, for its brevity.** C and D, the two
  variants without "be brief", scored the same as B and cost twice as much
  per item, because the reply is longer and every `log_answer` call carries
  it again. The first run's reading, that brevity cost the closing step, was
  wrong: brevity was innocent and the missing step was the whole story. The
  dropped "refunds" alternative did not recur under B in five runs.
- **BYO is not a floor, and the harness is not a ceiling.** D matched C on
  these five. The harness's edge is the check it can enforce and the cost its
  prompt saves, not better judgment. Section 9 of the design now says so.

So the default stays `--prompt harness` with the check on. `lean` stays
available as a measured alternative, not a recommendation.

## Confirmation on the full set

Alpenglow, Sonnet, harness prompt with the check, one pass each:

| set | passed | first-turn | capture | clean | cents/item |
|---|---|---|---|---|---|
| realistic (23) | 21/23 | 91% | 100% | 100% | 2.55 |
| trap (11) | 10/11 | 75% | 100% | 100% | 2.29 |

Realistic went from 19 to 21 with capture and clean at 100%, at the same
cost as before. The three misses are all the shape of the answer, not the
loop:

- `returns_by_quarter_h2_2024` again: the reply queried refunds and reported
  "refund value", and the item asked for the substring "refunds". The item
  now asks for "refund"; a model that names the alternative by querying it
  has done more than the disclosure asks.
- `new_customers_paid_search_2025_q1`: the reply gave January, February and
  March, correct and sourced, and the item wants the quarter's total. The
  scorer is right to be strict here, since the person asked for a quarter,
  but this is a judgment miss, not a wrong number.
- `orders_no_window` in the trap set: "how many orders have we had?" The
  model read that as all-time, ran a second query with explicit dates, and
  answered the full history, correctly disclosed. The item expects the
  tenant's default window. The same item failed the same way in one of the
  two runs on 2026-09-11, so it is the model's judgment against the
  registry's, and an honest reading of the question could go either way.

Nothing in either set regressed, and nothing skipped the check.
