# Golden authoring: what the drafter had to learn

2026-09-22. Step 5 of the build sequence: `understory draft-golden`,
`understory verify`, and the `golden-interview` skill. The drafter writes a
trap set from the catalog and the traps registry with no warehouse and no
model; `fill` then snapshots the numbers. Run on all four fake tenants, every
drafted set filled without a mismatch and passed the deterministic eval in
full: alpenglow 52 items, bristlecone 25, meridian 38, white_cube 86.

Getting there took four rules, each from a drafted item that failed.

1. **Pick a phrase no candidate covers.** A phrase inside the label of the
   metric the spec names is masked before matching (design 5), unless the
   metric is one of the trap's own candidates. "revenue" against
   `gross_revenue` never fires; "sales" does. The drafter takes the trap's
   first phrase that sits inside no candidate's name or label.
2. **A metric whose name is an ask phrase cannot be asked about.** white_cube
   has a metric called `revenue` and an `ask` on the phrase "revenue". Every
   question about that metric is stopped, by construction. So each template
   runs through the traps check before it is written, and one the registry
   would stop is drafted as the ask it will get, answered with the metric the
   spec already names. The same pass caught "recurring revenue", a `prefer`
   phrase that contains the `ask` phrase.
3. **A prefer needs somewhere to swap to.** A dimension-role item names the
   non-preferred candidate on a metric that carries both, so the prefer can
   apply and the trap's `disclose` text is what comes back. When no metric
   carries the preferred dimension beside another candidate, the prefer is
   bounded by the catalog (the fix from the realistic set), keeps what was
   named, and says why; the item then expects "does not carry" instead. Three
   candidates with no metric carrying all three is the common case, which is
   why the pairing is preferred-plus-one, not all.
4. **A cumulative metric is a series.** MetricFlow refuses a cumulative metric
   over a trailing window with no `metric_time` in the group-by, so `wau`
   drafts as a weekly series rather than a monthly total.

Two things about the numbers. The drafted question wording is flat on
purpose ("What was units in May 2026?"), the same convention as the realistic
drafter: a person rewrites it, and `source` keeps the lineage. And every item
asks about the last full month of the data window, so the snapshot does not
move when the fake data is regenerated with a later end date, until the
window's last full month itself moves.

`verify` edits the flag in place, where `dump_items` would put it, so a
verified file still round-trips through the dumper unchanged.
