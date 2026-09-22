"""The volume check: did the warehouse load every period the window touches?

A number over a period whose rows only partly arrived is a plausible wrong
number, and nothing in the semantic layer can see it. This check looks at the
row count per period on each time dimension the query reads, and when a period
inside the window holds far fewer rows than the same period in the weeks around
it, the answer carries a disclosure saying so. The number is never changed.

What counts as a dropout:

- The series is bucketed by the time dimension's own spacing: daily tables by
  day, a weekly feed by week. Spacing is the median gap between dates present.
- A bucket is compared with the same position in neighbouring cycles, so a
  Sunday is judged against other Sundays and a week against other weeks. That
  keeps weekly seasonality from firing the check on every quiet weekend.
- The baseline is the smaller of the medians before and after, with at least
  two neighbours on each side. A series that ramps up from nothing, or tails
  off at the end, is not a dropout; only a dip between two healthy stretches is.
- A bucket is low when it holds under `low` of its baseline, and the baseline
  itself has at least `min_rows`. Sparse tables are too noisy to judge.
- On a daily feed a single low day is indistinguishable from a public holiday,
  so daily dropouts are reported only as runs of two or more days. A weekly or
  coarser feed reports a single low bucket, since one bucket is a whole week.

The first and last buckets are never judged: both are partial by nature.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

LOW = 0.5
"""A bucket under this share of its baseline is low."""

MIN_ROWS = 20
"""Baselines below this are too sparse to judge."""

NEIGHBOURS = 4
"""Cycles looked at on each side of a bucket."""

_MIN_BUCKETS = 10


@dataclass(frozen=True)
class Dropout:
    """A run of low buckets on one time dimension."""

    start: date
    """First day of the first low bucket."""
    end: date
    """Last day of the last low bucket, inclusive."""
    spacing: int
    """Days per bucket: 1 for a daily table, 7 for a weekly feed."""
    rows: int
    """Rows actually present over the run."""
    expected: float
    """Rows the baselines say the run should hold."""

    @property
    def shortfall(self) -> float:
        """Share of the expected rows that are missing, 0 to 1."""
        if self.expected <= 0:
            return 0.0
        return max(0.0, 1.0 - self.rows / self.expected)

    def overlaps(self, start: date, end: date) -> bool:
        return self.start <= end and self.end >= start


def find_dropouts(
    series: list[tuple[date, int]],
    *,
    low: float = LOW,
    min_rows: int = MIN_ROWS,
    neighbours: int = NEIGHBOURS,
) -> list[Dropout]:
    """Runs of low buckets in a (date, row count) series sorted by date."""
    if len(series) < _MIN_BUCKETS:
        return []
    counts = dict(series)
    dates = sorted(counts)
    spacing = _spacing(dates)
    period = 7 if spacing == 1 else spacing

    flagged: list[tuple[date, int, float]] = []
    d = dates[0] + timedelta(days=spacing)
    last = dates[-1]
    while d < last:
        before = [
            counts[x]
            for j in range(1, neighbours + 1)
            if (x := d - timedelta(days=j * period)) in counts
        ]
        after = [
            counts[x]
            for j in range(1, neighbours + 1)
            if (x := d + timedelta(days=j * period)) in counts
        ]
        if len(before) >= 2 and len(after) >= 2:
            baseline = min(statistics.median(before), statistics.median(after))
            if baseline >= min_rows and counts.get(d, 0) < low * baseline:
                flagged.append((d, counts.get(d, 0), baseline))
        d += timedelta(days=spacing)

    runs: list[list[tuple[date, int, float]]] = []
    for item in flagged:
        if runs and (item[0] - runs[-1][-1][0]).days == spacing:
            runs[-1].append(item)
        else:
            runs.append([item])

    out: list[Dropout] = []
    for run in runs:
        if len(run) < 2 and spacing < 7:
            continue
        out.append(
            Dropout(
                start=run[0][0],
                end=run[-1][0] + timedelta(days=spacing - 1),
                spacing=spacing,
                rows=sum(r[1] for r in run),
                expected=float(sum(r[2] for r in run)),
            )
        )
    return out


def describe(dropout: Dropout, dimension: str) -> str:
    """One sentence for required_disclosures. Names the period and the shortfall."""
    if dropout.start == dropout.end:
        when = f"on {dropout.start.isoformat()}"
    else:
        when = f"from {dropout.start.isoformat()} to {dropout.end.isoformat()}"
    unit = "weeks" if dropout.spacing >= 7 else "days"
    return (
        f"Row volume on {dimension} {when} is {dropout.shortfall * 100:.0f}% below the same "
        f"{unit} around it ({dropout.rows:,} rows against about {dropout.expected:,.0f}), so "
        "that load may be incomplete. Numbers touching that period are a floor, not a total."
    )


def _spacing(dates: list[date]) -> int:
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    return max(1, int(statistics.median(gaps))) if gaps else 1


__all__ = ["LOW", "MIN_ROWS", "NEIGHBOURS", "Dropout", "describe", "find_dropouts"]
