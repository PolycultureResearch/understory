# First live eval against the fake companies

Polyculture Research | September 11, 2026

The first run of the Understory harness with a real model against the four fake_companies tenants. What was asked, what passed, what failed and why, and what to change. The cost side is in `token-use-2026-09-11.md`.

## 1. Headline numbers

Model: `anthropic/claude-sonnet-5` through OpenRouter. 47 golden questions across four tenants, run once each, the night of September 10.

| Tenant | Vertical | Passed | Coverage | Resolution | Answer | Disclosure | Capture |
|---|---|---|---|---|---|---|---|
| alpenglow | retail DTC | 7/11 | 100% | 73% | 100% | 50% | 82% |
| white_cube | B2C SaaS | 8/12 | 100% | 75% | 100% | 100% | 67% |
| meridian | B2B services | 7/12 | 100% | 75% | 100% | 50% | 92% |
| bristlecone | CPG wholesale | 7/12 | 100% | 75% | 89% | 25% | 92% |
| **All** | | **29/47** | **100%** | **74%** | **97%** | **56%** | **83%** |

Read those rates carefully, because the pass rate understates how well the system did and two of the rates overstate it.

- **No wrong number was stated.** Every figure the model gave came from a tool result, with one exception that the number check caught (section 4.3).
- **Every clarification worked.** All 15 questions expected to trigger a trap asked the right trap on the first turn and resolved with the right metric on the second.
- **The 18 failures split into five patterns**, and only one of them is the model doing the wrong thing. Ten are the model refusing correctly in a way the eval did not credit. Four are brittle golden strings. One is a grain choice the question left open. One is a real capture miss. The rest are side effects of a bug in the number check.
- **Answer accuracy is a bit lower than 97%.** One item passed because the expected number appeared in the model's explanation of why it was dropping that number (section 4.3).

A second run after the eval fixes was cut off partway by OpenRouter running out of credit, so it is not reported here beyond one figure: alpenglow completed 10 of 11 with the only failure being the credit error.

## 2. How the questions and expected answers were built

The golden sets live in `tenants/<name>/golden/questions.yml`, 11 or 12 items per tenant. They were written by hand against each tenant's metrics, by an agent working from the semantic manifest, the traps registry, and the context document, following the recipe in the MVP design (section 10). Each tenant covers the same seven situations:

| Situation | What it tests | Example |
|---|---|---|
| Covered question | Metric named directly, explicit window. Must not trip a collision even though the metric name contains a collision phrase. | "What was net revenue by month from January through March 2025?" |
| Main collision | The tenant's most ambiguous phrase, policy `ask`. Two turns: first must ask, second answers. | "How were sales in March 2025?" |
| Prefer policy | A collision or dimension role that resolves silently with a disclosure. | "Net revenue by country for the first quarter of 2025" (order country wins over customer country) |
| Dimension-role ask | Same word on two entities that mean different things. | "Bookings by source" (lead source, deal source, or won-deal source) |
| Unanswerable | A phrase declared out of scope in `traps.yml`, two per tenant. | "What is our profit by SKU?" |
| Invalid dimension | Valid metric, a dimension it does not have. | "MRR broken down by industry" |
| No time window | Tests the `default_window` convention: ask on three tenants, disclose on bristlecone. | "How many orders have we had?" |
| Value lookup | Needs `search_dimension_values` to turn a word into a stored value. | "Trials started in Germany" (stored as `DE`) |

Every item carries four things: the question, a `spec` (the exact `query_metrics` call the question should resolve to), an `expected` block, and notes on what it tests.

**Expected status and traps** were written by hand from the registry. They are the part of the golden set that carries independent judgment.

**Expected numbers** were produced by running each item's spec through `Service.query_metrics` against the seed-deterministic DuckDB files and recording the returned values, monetary values to two decimals and ratios to four. This means answer accuracy measures whether the model rendered what the semantic layer returned, not whether the semantic layer is right. The semantic layer's own correctness is a separate question that fake_companies' ground truth can answer later. Windows are explicit wherever numbers are recorded so regeneration with a later end date does not move them.

**Expected disclosures** are short substrings the answer must contain, chosen from the registry's disclosure text.

Before any live run, the deterministic runner replays every spec through the server with no model and checks status, trap ids, numbers, and disclosures. It passed 47 of 47, so every failure below is about the model and the eval, not the server.

## 3. How scoring works

Five per-item metrics, each true, false, or not applicable:

- **Coverage.** The answer came through `query_metrics` rather than `run_sql` or a refusal, and used the expected metrics. Only applies to items expected to resolve.
- **Resolution.** Asked exactly the expected traps when it should, and did not ask when it should not. For unanswerable and invalid items in this run, the tool status had to appear.
- **Answer.** Every expected number appears in the reply, within rounding or 0.5%.
- **Disclosure.** Every expected substring appears in the reply, case-insensitive.
- **Capture.** `log_answer` was called at least once.
- **Clean.** Added after this run. False when the reply narrates the number check. Not part of the rates in section 1.

An item passes when every applicable metric is true. Rates are over applicable items only, so disclosure rate is over the four or five items per tenant that declared one.

## 4. What happened, by pattern

### 4.1 The model refuses from context, so the server never sees it (10 items)

Every unanswerable and invalid question failed the same way. The model called `get_context`, read the "what the data cannot answer" section, and refused in prose without calling `query_metrics`. The answers were right. Three of them:

> There's no profit metric and no SKU dimension in this data. The governed model only has gross margin and it can only be sliced by category, not by SKU.

> Bookings cannot be broken down by region. In this data, region is only available on deals by their creation date, while bookings is measured by close date and only supports breakdowns by source, industry, and size tier.

> The governed data explicitly does not provide forecasts. I can't generate a projection for next quarter, since that would mean inventing a number not backed by any tool result.

The eval expected the `unanswerable` or `invalid` status from the tool and marked all ten as failed. The scoring has since been changed to credit a prose refusal.

The finding underneath is more important than the scoring. In a bring-your-own-chatbot deployment, a capable model that reads a good context document will refuse most out-of-scope questions without touching the traps registry. That is the right answer for the user and a hole in the improvement loop: nothing reaches telemetry, so `mart_semantic_backlog` never learns that people keep asking for profit by SKU. The harness prompt now asks the model to call `log_answer` even when it never queried, which records the draft. Whether to add a dedicated refusal event, or to move the unanswerable list out of the context document so the model has to ask, is a decision for after the first client. It is recorded in the design doc's open questions.

Items: alpenglow `profit_by_sku_unanswerable`, `ltv_unanswerable`, `promo_dimension_invalid`; white_cube `ltv_unanswerable`, `nps_unanswerable`, `mrr_by_industry_invalid`; meridian `quota_unanswerable`, `forecast_unanswerable`, `bookings_by_region_invalid`; bristlecone `gross_margin_unanswerable`, `market_share_unanswerable`, `cases_by_banner_invalid`. Two of these also failed a disclosure substring, covered next.

### 4.2 Golden disclosure strings were too literal (4 items, plus 2 overlapping with 4.1)

The model stated every disclosure it was given, in its own words, and the substring check missed the paraphrase:

| Item | Expected substring | What the model wrote |
|---|---|---|
| alpenglow `orders_no_window` | "30 days" | "trailing-30-day window" |
| meridian `bookings_no_window` | "90 days" | "trailing-90-day window" |
| bristlecone `cases_no_window` | "90 days" | "trailing 90-day window" |
| meridian `pipeline_prefer_march_2025` | "deals open on the day" | "deals open on that day" |
| bristlecone `gross_margin_unanswerable` | "no COGS" | "no cost-of-goods information" |
| alpenglow `profit_by_sku_unanswerable` | "category grain" | "only be sliced by category" |

Matching now ignores hyphens, and the six substrings were loosened to the word that matters. The disclosure rate of 56% is therefore mostly a measurement artifact. Reading the answers, every required disclosure was present in all 35 answered items.

### 4.3 The number check flagged dates, and the model told the user about it (22 items affected, 5 visibly)

`log_answer` checks that every number in the draft traces to a tool result. Its number extractor read the day in "Mar 31" or "2025-03-31" as the number 31 and reported it as unsourced. This happened on 22 of the 35 answered items. It did not fail any item directly, because the capture metric only asks whether `log_answer` was called. But it had three effects:

- Extra requests. The model often called `log_answer` a second time after rewording, which costs a full request each time.
- Leaked feedback. In five answers the model narrated the check to the user. White Cube's signups-by-country answer opens with "That's just the '31' in the date 'Mar 31' being flagged, not a real issue, but let me rephrase to avoid confusion." Bristlecone's spend and category answers and Meridian's bookings-by-source answer do the same. This is the worst user-facing defect in the run.
- One false pass. On `trials_germany_h1_2025` the model summed six monthly values to 315, which is arithmetic the prompt forbids. The check correctly flagged 315 as unsourced. The model then wrote "The 315 total wasn't in a tool result, so I'll drop that computed sum and report the monthly figures instead" and listed the months. The eval found 315 in the reply and marked the answer correct. The check worked exactly as designed. The eval and the prompt did not.

Both date forms are now stripped before extraction, and the harness prompt is being changed to forbid mentioning the check in the reply. The false pass is a scoring gap: the answer check should ignore numbers that appear in a sentence about dropping them, or better, the model should not write that sentence.

### 4.4 The question left the grain open (1 item)

Bristlecone `pos_units_greenfields_h1_2025` asked for "POS units at greenfields between January and June 2025" and the golden set expected the six-month total. The model grouped by month and gave six figures that sum to the expected total. Correct answer, different shape. The question now says "total".

### 4.5 One real capture miss (1 item)

White Cube `mrr_by_month_h1_2025` resolved with all six correct numbers and the model replied without calling `log_answer`. This is the behavior the capture metric exists to measure. Nothing to fix in the eval. The prompt asks for the call in its last rule, and one miss in 35 answered items is the baseline to watch.

### 4.6 What worked

- **Clarification round trips, 15 of 15.** Every collision and dimension-role item asked the declared trap on the first turn, presented the options with hints, and on the second turn queried the chosen metric. Alpenglow's "How did returns look" correctly asked return rate versus returned units; Meridian's "revenue" correctly offered bookings, invoiced revenue, and collected cash.
- **Prefer policies disclosed.** "Country reflects where the order shipped, not the customer's residence" appeared unprompted in the alpenglow answer. Bristlecone's "topline" resolved to wholesale net revenue with "net of trade discounts" stated.
- **Value lookup.** The model called `search_dimension_values` for hoodies and for Germany without being told to, and once for "US" to confirm the value existed.
- **Time anchoring.** Every answered item stated that the window was anchored to the latest available data rather than today. That disclosure comes from the server, and the model never dropped it.
- **The number check caught the one arithmetic violation**, as above.

### 4.7 Behavior worth noting but not failing

- **Redundant `list_metrics`.** In 11 of 47 items the model called `list_metrics` immediately after or before `get_context`, which already lists every metric. One extra request per item.
- **Timing.** Refusals took 5 to 9 seconds, single-turn answers 10 to 16, two-turn clarifications 18 to 38. A user in a chatbot waits the same.
- **Ratios were rendered as percentages** with the raw ratio in parentheses ("58.2% (gross margin ÷ gross revenue)", "21.6% (0.2161)"). Good for the reader, and the number check handles both forms.

## 5. Per-item results

Outcome is what happened. Verdict is the eval's original scoring, then what it should have been once the artifacts above are removed.

### alpenglow (retail DTC)

| Item | Question | Expected | Outcome | Eval | Should be |
|---|---|---|---|---|---|
| net_revenue_by_month_q1_2025 | Net revenue by month, Jan to Mar 2025 | resolved | 3 correct numbers | pass | pass |
| sales_collision_march_2025 | How were sales in March 2025? | ask revenue | asked, resolved net revenue | pass | pass |
| net_revenue_by_country_prefer | Net revenue by country, Q1 2025 | resolved, order country | 4 numbers, disclosure stated | pass | pass |
| margin_collision_q2_2025 | What was our margin in Q2 2025? | ask margin | asked, resolved margin rate 58.2% | pass | pass |
| returns_collision_april_2025 | How did returns look in April 2025? | ask returns | asked, resolved return rate 6.36% | pass | pass |
| profit_by_sku_unanswerable | What is our profit by SKU? | unanswerable | refused from context | fail | pass (4.1, 4.2) |
| ltv_unanswerable | Lifetime value of our customers? | unanswerable | refused from context | fail | pass (4.1) |
| promo_dimension_invalid | Return rate for promo-code orders? | invalid | refused from context | fail | pass (4.1) |
| orders_no_window | How many orders have we had? | ask window | asked, resolved 15,996, "trailing-30-day" | fail | pass (4.2) |
| units_hoodies_h1_2025 | Units sold in hoodies, H1 2025 | resolved | value lookup, 14,775 | pass | pass |
| repeat_order_share_2025 | Share of 2025 orders from existing customers | resolved | 56.1% | pass | pass |

### white_cube (B2C SaaS)

| Item | Question | Expected | Outcome | Eval | Should be |
|---|---|---|---|---|---|
| net_new_mrr_by_month_q1_2025 | Net new MRR by month, Q1 2025 | resolved | 3 correct numbers | pass | pass |
| revenue_collision_march_2025 | Revenue in March 2025? | ask revenue | asked, resolved cash revenue | pass | pass |
| mrr_by_month_h1_2025 | MRR by month, H1 2025 | resolved | 6 correct numbers, no log_answer | fail | fail (4.5) |
| signups_by_country_prefer | Signups by country, Q1 2025 | resolved, signup country | correct, but reply opens with the "31" narration | pass | pass with a defect (4.3) |
| churn_collision_q2_2025 | Our churn in Q2 2025? | ask churn | asked, resolved churned MRR | pass | pass |
| active_users_collision_may_2025 | Active users in May 2025? | ask active users | asked, resolved DAU | pass | pass |
| ltv_unanswerable | Lifetime value of a customer? | unanswerable | refused from context | fail | pass (4.1) |
| nps_unanswerable | Our NPS this year? | unanswerable | refused from context | fail | pass (4.1) |
| mrr_by_industry_invalid | MRR by industry, Q1 2025 | invalid | refused from context | fail | pass (4.1) |
| signups_no_window | How many signups have we had? | ask window | asked, resolved 3,167 | pass | pass |
| trials_germany_h1_2025 | Trials started in Germany, H1 2025 | resolved | value lookup, monthly figures, summed total caught and dropped | pass | fail on answer shape (4.3) |
| trial_conversion_rate_q1_2025 | Trial conversion rate, Q1 2025 | resolved | 21.6% | pass | pass |

### meridian (B2B services)

| Item | Question | Expected | Outcome | Eval | Should be |
|---|---|---|---|---|---|
| bookings_by_month_q1_2025 | Bookings by month, Q1 2025 | resolved | 3 correct numbers | pass | pass |
| revenue_collision_q1_2025 | Revenue in Q1 2025? | ask revenue (3 options) | asked, resolved invoiced revenue | pass | pass |
| pipeline_prefer_march_2025 | Pipeline worth on last day of March 2025 | resolved, disclose | $27.2M, "deals open on that day" | fail | pass (4.2) |
| bookings_by_source_role | Bookings by source, Q1 2025 | ask source role | asked, resolved won-deal source; reply opens with the "31" narration | pass | pass with a defect (4.3) |
| deals_collision_february_2025 | Deals closed in February 2025? | ask deals | asked, resolved deals won, 92 | pass | pass |
| quota_unanswerable | Quota attainment for the team? | unanswerable | refused from context | fail | pass (4.1) |
| forecast_unanswerable | Forecast for next quarter? | unanswerable | refused from context | fail | pass (4.1) |
| bookings_by_region_invalid | Bookings by region, Q1 2025 | invalid | called describe_metric, refused with the right reason | fail | pass (4.1) |
| bookings_no_window | What were bookings? | ask window | asked, resolved $17.0M, "trailing-90-day" | fail | pass (4.2) |
| bookings_healthcare_q1_2025 | Bookings from healthcare clients, Q1 2025 | resolved | $1.83M | pass | pass |
| collected_cash_q1_2025 | Cash collected in Q1 2025 | resolved | $10.66M | pass | pass |
| win_rate_q1_2025 | Win rate in Q1 2025 | resolved | 7.0% | pass | pass |

### bristlecone (CPG wholesale)

| Item | Question | Expected | Outcome | Eval | Should be |
|---|---|---|---|---|---|
| net_revenue_by_month_q1_2025 | Wholesale net revenue by month, Q1 2025 | resolved | 3 correct numbers | pass | pass |
| volume_collision_march_2025 | How did volume look in March 2025? | ask units | asked, resolved shipped cases 47,010 | pass | pass |
| topline_prefer_march_2025 | Our topline in March 2025? | resolved, disclose | $1.66M, "net of trade discounts" | pass | pass |
| cases_by_category_role | Shipped cases by category, Q1 2025 | ask category role | asked, resolved shipment category; reply opens with the "31" narration | pass | pass with a defect (4.3) |
| spend_collision_q1_2025 | How much did we spend in Q1 2025? | ask spend | asked, resolved trade spend; reply opens with the "31" narration | pass | pass with a defect (4.3) |
| gross_margin_unanswerable | Gross margin by category? | unanswerable | refused from context | fail | pass (4.1, 4.2) |
| market_share_unanswerable | Market share in the tea category? | unanswerable | refused from context | fail | pass (4.1, 4.2) |
| cases_by_banner_invalid | Shipped cases by retailer banner, Q1 2025 | invalid | refused from context | fail | pass (4.1) |
| cases_no_window | How many cases did we ship? | resolved, disclose 90 days | 116,567, "trailing 90-day" | fail | pass (4.2) |
| pos_units_greenfields_h1_2025 | POS units at Greenfields, Jan to Jun 2025 | resolved, total | monthly figures summing to the total | fail | pass (4.4) |
| trade_spend_by_month_q1_2025 | Trade spend by month, Q1 2025 | resolved | correct, including two zero months | pass | pass |
| avg_case_price_q1_2025 | Realized average price per shipped case, Q1 2025 | resolved | $34.90 | pass | pass |

Rescored with the artifacts removed, the run is 44 of 47, with the three remaining failures being one capture miss, one answer-shape defect, and the four leaked-narration replies counted as a defect on otherwise passing items.

## 6. Recommendations

Grouped by where the change lives. Items marked done were made on September 11.

**Server**

- Strip ISO and month-name dates before the number check. Done.
- Consider a dedicated refusal path so a chatbot that refuses from context can still record it. The cheapest version is a `reason` field on `log_answer`. The stronger version is moving the unanswerable list out of `get_context` so the model has to call `query_metrics` and hit the trap. That trades a slower refusal for a logged one. Decide after the first client.
- Say in the `list_metrics` description that `get_context` already contains the list, to remove the redundant call.

**Harness**

- Never mention `log_answer` or its checks in the reply. In progress on the `harness-cost` branch.
- Call `log_answer` on refusals too, so they are captured. Done.
- Continue the conversation on the clarification turn rather than restarting. In progress.
- Forbid arithmetic on returned values more forcefully, or add a `sum` option to `query_metrics` so a total is a tool result rather than a temptation.

**Eval scoring**

- Credit a prose refusal for unanswerable and invalid items. Done. Keep reporting how many refusals reached the server, since that is the telemetry gap in numbers.
- Normalize hyphens and whitespace in disclosure matching. Done.
- Ignore expected numbers that appear only in a sentence about dropping them, and fail an item whose reply narrates the check. Done: a sixth metric, clean, is false when any sentence in the reply talks about the number check, and the answer check runs on the reply with those sentences removed. The Germany trials item now fails on both counts.
- Capture token usage and cost per item. In progress.

**Golden sets**

- Loosen the six literal substrings. Done.
- Say "total" when a total is expected. Done for Greenfields.
- Add a second model's run to each set before treating any rate as a baseline. One run of one model at default temperature is an anecdote.
- Add items the current sets do not cover: a question whose answer needs `run_sql`, a question with a value that does not exist ("sales in the West" on a country-only tenant), a multi-metric question, and a question with a where clause on a value that needs lookup.

**Process**

- Run the deterministic eval in CI on every push. It caught nothing here only because it was run before the live eval, which is the point.
- Run live evals on a cheap model on every deploy and on Sonnet weekly, with the budget guard on.

## 7. Caveats

One run, one model, default temperature. The rerun after fixes was cut off by the credit limit and is not comparable. Expected numbers are a regression check against the semantic layer, not independent truth. The golden sets were written by the same agent that had just read the traps registry, so they test the traps we declared and not the ones a real user would find. Real client questions will be the first honest test, which is why the telemetry loop matters more than this score.
