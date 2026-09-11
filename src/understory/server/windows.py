"""Time window resolution.

The chatbot may send explicit start and end dates, a grain, or a window id
chosen through the `default_window` convention trap. Everything anchors to the
latest date the data has, never to today. A festival that finished loading
three days ago should answer "last week" about the week it has, not about a
week with no rows.
"""

from __future__ import annotations

from datetime import date, timedelta

from understory.types import TimeSpec

WINDOW_IDS = (
    "trailing_7_days",
    "trailing_30_days",
    "trailing_90_days",
    "last_month",
    "last_quarter",
    "year_to_date",
)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def resolve_window(window: str, anchor: date) -> tuple[date, date]:
    """Return (start, end) inclusive for a window id anchored at `anchor`.

    Trailing windows end on the anchor. Calendar windows are the last complete
    period before the anchor's period, so "last month" on 2026-09-10 is August.
    """
    if window == "trailing_7_days":
        return anchor - timedelta(days=6), anchor
    if window == "trailing_30_days":
        return anchor - timedelta(days=29), anchor
    if window == "trailing_90_days":
        return anchor - timedelta(days=89), anchor
    if window == "last_month":
        this = _month_start(anchor)
        end = this - timedelta(days=1)
        return _month_start(end), end
    if window == "last_quarter":
        this = _quarter_start(anchor)
        end = this - timedelta(days=1)
        return _quarter_start(end), end
    if window == "year_to_date":
        return date(anchor.year, 1, 1), anchor
    raise ValueError(f"unknown window '{window}'; known: {', '.join(WINDOW_IDS)}")


def apply_anchor(time: TimeSpec, anchor: date | None, window: str | None = None) -> TimeSpec:
    """Fill in missing dates. Explicit dates win. A window id fills both.

    With no window and no dates, `end` becomes the anchor and `start` is left
    open, which MetricFlow treats as unbounded. An `end` after the anchor is
    pulled back to the anchor so an answer never claims days with no data.
    """
    start, end = time.start, time.end
    if window and start is None and end is None and anchor is not None:
        start, end = resolve_window(window, anchor)
    if anchor is not None:
        if end is None or end > anchor:
            end = anchor
    return TimeSpec(grain=time.grain, start=start, end=end)


def describe(time: TimeSpec, anchor: date | None, window: str | None = None) -> str:
    """One sentence for required_disclosures about what window was used."""
    parts: list[str] = []
    if window:
        parts.append(f"Window is {window.replace('_', ' ')}")
    if time.start and time.end:
        parts.append(f"{time.start.isoformat()} to {time.end.isoformat()}")
    elif time.end:
        parts.append(f"through {time.end.isoformat()}")
    if anchor is not None:
        parts.append(f"anchored to the latest available data ({anchor.isoformat()}), not today")
    return (", ".join(parts) + ".") if parts else ""
