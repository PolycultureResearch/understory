# Meridian Partners

## What the company does

Meridian Partners is a professional-services firm that sells project engagements to business clients. Its accounts are companies in manufacturing, healthcare, logistics, finance, and retail, spread across the northeast, midwest, south, and west of the United States, from small businesses to enterprises. There is no web funnel and no subscription. Growth comes from a sales team working a pipeline, and money comes from contracts that are invoiced and then collected. History starts in March 2024, in US dollars. It is a weekday business: almost nothing happens on weekends.

## How the money flows

Leads come from four sources: outbound prospecting, referrals, events, and the website. Every lead opens a deal against an account, new or existing. A deal moves through stages in order: lead, qualified, proposal, negotiation, and then closed won or closed lost. Each stage takes days to weeks and roughly half of deals advance at each gate, so a typical win takes a couple of months from creation to close. A won deal becomes a contract: about forty percent of the amount is invoiced at signing and the balance at delivery, roughly two months later. Invoices are due in thirty days and a fair share are paid late.

## Metrics in plain words

Leads counts deals created, dated by creation day. Qualified deals, proposals sent, and negotiations count deals entering each stage, dated by the day they entered it. Deals won and deals lost count closes, dated by close day, and closed deals is their sum.

Win rate is deals won divided by closed deals, both by close date. Bookings is the value of deals won by close date, and average deal size is bookings divided by deals won. Sales cycle days is the mean number of days from creation to close for deals won, dated by close.

Open deals and open pipeline value are daily snapshots of what was open on each day. They are levels: read them at a point in time and never add them across days.

Invoiced revenue is the value of invoices issued, by issue date. Collected cash is payments received, by paid date. Bookings, invoiced revenue, and collected cash are three different numbers describing the same contracts at different moments, and over any short window they will not agree.

## Gotchas

Wins take longer to close than losses, so a change in pipeline health shows up in losses first and in win rate weeks later; read win rate over a month or more. Stage metrics count entries, so a deal that enters a stage twice is counted twice.

Source, industry, and size tier are available on most metrics, but region is only on deals by creation date. Stage on the deals table is the current stage, not a history. Bookings by creation month and bookings by close month are different questions; the metrics use close date.

Roughly one lead in five comes from an account Meridian already works with, but new versus existing account is not a dimension, so leads and bookings cannot be split that way. Because weekends are nearly empty, a week-over-week comparison should line up whole weeks, and a month with five Mondays will look stronger than one with four for no business reason. Late payment is normal: about a quarter of invoices are paid a few weeks after the due date, so collected cash trails invoiced revenue by more than the thirty-day terms suggest.

## What the data cannot answer

There is no delivered or recognized revenue, no hours or utilization, no cost or margin, and no headcount. Deals carry no owner, so performance by salesperson is not available. Outstanding receivables and aging are not modeled as metrics, although invoices carry a status. Lost reasons are not recorded, and forecasts are not something this data provides.
