from datetime import date

from understory.server.session import SessionStore, extract_numbers, review_answer
from understory.server.windows import apply_anchor, describe, resolve_window
from understory.types import Column, Result, TimeSpec


def _result(rows, cols=("metric_time__month", "net_revenue")):
    return Result(columns=[Column(name=c, type="x") for c in cols], rows=rows, row_count=len(rows))


def test_resolve_windows():
    a = date(2026, 9, 10)
    assert resolve_window("trailing_7_days", a) == (date(2026, 9, 4), a)
    assert resolve_window("last_month", a) == (date(2026, 8, 1), date(2026, 8, 31))
    assert resolve_window("last_quarter", a) == (date(2026, 4, 1), date(2026, 6, 30))
    assert resolve_window("year_to_date", a) == (date(2026, 1, 1), a)


def test_apply_anchor_never_after_data():
    anchor = date(2026, 9, 1)
    t = apply_anchor(TimeSpec(end=date(2026, 12, 31)), anchor)
    assert t.end == anchor
    t = apply_anchor(TimeSpec(), anchor, window="trailing_30_days")
    assert (t.start, t.end) == (date(2026, 8, 3), anchor)
    t = apply_anchor(TimeSpec(start=date(2026, 1, 1)), anchor)
    assert (t.start, t.end) == (date(2026, 1, 1), anchor)
    assert "not today" in describe(t, anchor)


def test_extract_numbers():
    nums = dict(extract_numbers("Revenue was $1,234,567.89, up 8.5% to 1.2M in 2025."))
    assert nums["$1,234,567.89"] == 1234567.89
    assert nums["8.5%"] == 8.5
    assert nums["1.2M"] == 1_200_000
    assert nums["2025"] == 2025
    dated = dict(extract_numbers("From 2025-03-01 to 2025-03-31, orders were 16,016."))
    assert list(dated) == ["16,016"]


def test_review_answer_traces_numbers():
    store = SessionStore()
    s = store.get("conn-1", user_hash="u")
    s.remember("query_metrics", _result([["2025-01", 1234567.89], ["2025-02", 0.085]]))
    s.owe(["Quarters are fiscal (February to January)."])

    ok = review_answer(
        "Net revenue was about $1.23M in January, an 8.5% rate. Note quarters are fiscal, "
        "February to January.",
        s,
    )
    assert ok.status == "pass", ok
    assert ok.disclosures_missing == []

    bad = review_answer("Net revenue was $9,999,999 in January across 3 regions in 2025.", s)
    assert bad.status == "unsourced_numbers"
    assert [n.number for n in bad.unsourced] == ["$9,999,999"]
    assert bad.disclosures_missing


def test_review_without_results():
    s = SessionStore().get("conn-2")
    assert review_answer("Anything 123,456", s).status == "no_results_in_session"


def test_sessions_expire():
    store = SessionStore(ttl_s=0)
    store.get("a")
    store.get("b")
    assert len(store) == 1
