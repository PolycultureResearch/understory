---
name: golden-interview
description: Turn an hour with a client's data owner into golden items. Use when drafting a tenant's trap or realistic set from an interview, a help-channel export, or a list of questions people actually ask.
---

# Golden interview

The third source of a client's golden sets (design 10.3): the questions people
actually ask, in their words, from the person who answers them today. The
catalog and the traps registry are already drafted by `understory draft-golden`
before this starts; this session adds what neither can know.

Work in the tenant directory. Read `traps.yml` and `golden/questions.yml`
first, so you ask about what is missing rather than what is drafted.

## The interview

One hour. Four questions, in this order, each until it runs dry:

1. **What do people ask you every week?** Take each question verbatim. Ask
   who asks it and what number they take away. These become `event` and
   `quiet` items in the realistic set, worded as the asker words them.
2. **Which words mean two things here?** Revenue, margin, customers, active,
   churn, region. For each: the two meanings, which one a board deck means,
   and whether a wrong reading has ever caused a bad decision. A word with a
   safe default is a `prefer`; a word that has burned them is an `ask`, and
   the story is its `why`. These go to `traps.yml`, not the golden set.
3. **What do you refuse to answer, and what do you say instead?** Each is an
   `unanswerable` trap with its reason, and a trap-set item that expects the
   refusal.
4. **Which twenty numbers matter?** The ones on the board deck, the weekly
   email, the investor update. Get the figure and the report it comes from.
   These are the items worth `verified: true`, and the only ones the owner
   needs to check.

Note every phrase the owner uses for a metric that the catalog does not
list. Those are synonyms for the semantic layer, not golden items.

## From transcript to items

For each question from (1) and (4), write one item in `golden/realistic.yml`
(or `questions.yml` for (3)) in the file's own shape: the question verbatim,
`kind`, `source: interview`, a `spec` you are sure of, and `expected.status`
as your judgment of what a correct run does. Leave `numbers` empty.

Then, in the tenant directory:

```bash
uv run understory fill --tenant . --set realistic
uv run understory eval --tenant . --set realistic --deterministic
```

`fill` snapshots numbers onto items whose run agrees with your judgment and
leaves the rest unfilled with a note; the eval lists them. An unfilled item is
either a wrong spec (fix it) or open server work (keep it, say so in `notes`).

For the numbers from (4): compare the snapshot with the figure the owner gave.
A match, or a difference you can explain (window, a filter the report bakes
in), is verified:

```bash
uv run understory verify --tenant . --set realistic <id> [<id>...]
```

A difference you cannot explain is a finding. Record it in `notes` on the item,
leave it unverified, and raise it with the owner before the deploy.

Done when: every question from the interview is an item or a synonym note,
every trap from (2) and (3) is in `traps.yml`, both sets pass the
deterministic eval or carry a note saying why not, and the owner's numbers are
verified or written up as findings.
