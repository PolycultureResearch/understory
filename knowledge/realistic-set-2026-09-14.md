# Realistic set, first draft: what the snapshot found

Date: 2026-09-14. Step 4 of the pre-pilot build order. 85 items across the four fake tenants, drafted with `understory draft-realistic`, rewritten by hand, numbers snapshotted with `understory fill`. Deterministic run on the first draft: 78 of 85 pass. Same day, after the matcher fix below: 85 of 85.

| tenant | items | pass | open |
|---|---|---|---|
| alpenglow | 22 | 22 | 0 |
| bristlecone | 18 | 15 | 3 |
| meridian | 20 | 18 | 2 |
| white_cube | 25 | 23 | 2 |

## Open items were one server behaviour (fixed the same day)

Six of the seven failures have the same shape. The question uses a trap phrase (`sales`, `spend`, `source`, `plan`) and the chatbot sends a spec whose where clause or group-by only fits the metric it chose. The `prefer` policy swaps in the preferred candidate anyway, and the preferred candidate does not carry that dimension. MetricFlow rejects the query and the refusal carries MetricFlow's own error text rather than Understory's message.

- bristlecone `pos_dollars_whole_earth_2025_q1`: `sales` swaps POS dollars for wholesale net revenue, which has no retailer banner.
- bristlecone `marketing_spend_paid_social_2025_05_06` and `marketing_spend_podcasts_2025_06`: `spend` swaps marketing spend for trade spend, which has no ad channel.
- meridian `leads_by_source_2024_q3`: the `source` role swaps the deal's source for the won deal's source, which leads do not carry.
- white_cube `churned_mrr_professional_2026_05` and `trial_conversion_rate_professional_2025_q3`: the `plan` role swaps the movement's or trial's plan for the subscription day's plan.

Fixed in `traps/match.py` and `server/service.py`: a prefer is bounded by the catalog and does not apply when the preferred candidate lacks a dimension the spec uses; the spec's candidate stays and the disclosure says why. Roles now run before collisions so the collision judges against the settled dimension. The service re-validates the spec after traps, so a rewrite can never reach MetricFlow with a dimension the metric lacks. Each shape has a trap-set item (bristlecone, meridian, white_cube now 14 items each).

The seventh, meridian `sales_cycle_by_month_2025_04_07`, was a phrase problem: "sales cycle" contains the revenue ask's phrase. Fixed by covering: a phrase that sits inside the name or label of a metric the spec names, outside the trap's candidates, does not fire. The spec-text fallback in `_fires` was dead after that and is gone.

## Disclosures that describe a swap that did not happen

Three items resolved with the right metric but carried a prefer disclosure claiming the phrase was read as the preferred candidate:

- white_cube `direct_conversions_2024_09_10`: "'conversions' is read as Trial Conversion Rate", metric run was direct conversions.
- white_cube `net_new_mrr_explicit_2025_q1`: "'mrr' is read as MRR", metric run was net new MRR.
- meridian `deals_lost_2025_q2`: "'deals' is read as Deals Won", metric run was deals lost.

The disclosure fired whenever the phrase matched, even when `_apply_metric` changed nothing. Fixed: a prefer discloses when the spec ends on the preferred candidate, says "as named" when an explicit non-preferred candidate was kept, and says nothing (and does not count as fired) when the spec names no candidate. Still worth an `absent_disclosures` field on golden items so the trap set can forbid a disclosure; the schema cannot yet.

## Plausible wrong numbers from the fake semantic layers

Two drafted items returned 1.0 for a ratio metric grouped or filtered through a joined entity:

- alpenglow `conversion_rate` by `customer__first_channel`: every channel 1.0.
- white_cube `visit_signup_rate` where `user__device = mobile`: 1.0.

Understory cannot tell these from a real number. They were dropped from the set and the white_cube one replaced with a count. Root cause (investigated 2026-09-14): the `sessions` semantic model in retail_dtc and b2c_saas carries a customer or user entity that the generator sets only on the converting session, so 96% of sessions are null on it; grouped or filtered through that entity the denominator collapses to the numerator. Only those two links are sparse. Recommended fake_companies fix, verified on a scratch copy: drop the sparse entity from `sessions`, add a `converting_sessions` (or `signup_sessions`) measure as a case expression on the id, and point the ratio's numerator at it; the ratio becomes single-model, groupable by session dimensions, and the customer-side dimensions become unreachable so MetricFlow fails loudly. Applied in fake_companies PR #7 (2026-09-15); the alpenglow and white_cube manifests were regenerated from its `dbt parse` output and the two dropped items are back in the realistic sets as `conversion_rate_by_channel_2024_09` and `visit_signup_rate_mobile_2026_01_02`, grouped and filtered through the session's own dimensions.

A cousin: white_cube `trial_conversion_rate` where `trial__plan = professional` is 1.0 because a trial's plan is only set when it converts. That is data semantics, not a join; the realistic item was changed to a count of conversions by plan. It is the shape of error the trap set cannot catch, and an argument for the critic in roadmap 1.5.

## Two asks, recorded as the misses they are

`revenue` asks on meridian and white_cube, and the `units` ask on bristlecone, appear in the realistic set with a second-turn answer. white_cube `cash_revenue_ask_2025_03` is the sharp one: the person said "cash revenue, not MRR" and still got asked, because `revenue` is both the phrase and a candidate name and cannot be masked. The first-turn answer rate will count it against us, which is the point.

## Not drafted: fault items

Duplicates and loading delays are invisible in a static warehouse (staging dedupes on `_loaded_at`). A volume dropout only shows as a smaller number. Fault items need a volume or freshness check in the server and probably an as-of build of the fake data. Deferred within step 4.
