from datetime import date

from understory.server.session import Session, SessionStore, extract_numbers, review_answer
from understory.server.windows import apply_anchor, describe, resolve_window
from understory.types import Column, Result, TimeSpec


def _result(rows, cols=("metric_time__month", "net_revenue")):
    return Result(columns=[Column(name=c, type="x") for c in cols], rows=rows, row_count=len(rows))


def test_resolve_windows():
    a = date(2026, 9, 10)
    assert resolve_window("trailing_7_days", a) == (date(2026, 9, 4), a)
    assert resolve_window("last_week", a) == (date(2026, 8, 31), date(2026, 9, 6))
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
    prose = dict(
        extract_numbers(
            "Q1 2025 (Jan 1 – Mar 31, 2025) and 30 June 2026: bookings were $17,040,100."
        )
    )
    assert "$17,040,100" in prose
    assert not {"1", "31", "30"} & set(prose)


def test_review_answer_traces_numbers():
    store = SessionStore()
    s = store.get("conn-1", user_hash="u")
    s.remember(
        "query_metrics",
        _result([["2025-01", 1234567.89], ["2025-02", 0.085]]),
        disclosures=["Quarters are fiscal (February to January)."],
    )

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


def _two_months() -> Session:
    s = SessionStore().get("conn-arith")
    s.remember("query_metrics", _result([["2025-01", 980000.0]]), disclosures=["Net of returns."])
    s.remember("query_metrics", _result([["2025-02", 933576.0]]), disclosures=["Net of returns."])
    return s


def test_arithmetic_over_numbers_the_draft_sources_passes():
    """The grill's case: two months and the change between them."""
    s = _two_months()
    review = review_answer(
        "Net revenue was $980,000 in January and $933,576 in February, down $46,424 "
        "(-4.7%), or $1,913,576 across both. The ratio of the two is 0.95. Net of returns.",
        s,
    )
    assert review.status == "pass", review
    derived = {c.number: c.derived for c in review.checked if c.derived}
    assert derived == {
        "$46,424": "difference",
        "-4.7%": "percent change",
        "$1,913,576": "sum",
        "0.95": "ratio",
    }
    assert all(c.result_id is None for c in review.checked if c.derived)


def test_arithmetic_needs_its_operands_in_the_draft():
    s = _two_months()
    review = review_answer("Net revenue fell 4.7% to $933,576 in February. Net of returns.", s)
    assert review.status == "unsourced_numbers"
    assert [n.number for n in review.unsourced] == ["4.7%"]

    wrong = review_answer("It went from $980,000 to $933,576, down 7.4%. Net of returns.", s)
    assert [n.number for n in wrong.unsourced] == ["7.4%"]


def test_a_column_total_and_a_share_of_it_pass():
    s = SessionStore().get("conn-total")
    s.remember("query_metrics", _result([["US", 600.0], ["CA", 300.0], ["GB", 100.0]]))
    review = review_answer("The US led with 600 of 1,000 orders, 60% of the total.", s)
    assert review.status == "pass", review
    assert [c.derived for c in review.checked] == [
        None,
        "total of net_revenue",
        "share of net_revenue",
    ]


def test_disclosures_are_scoped_to_the_results_a_draft_quotes():
    s = SessionStore().get("conn-scope")
    s.remember(
        "query_metrics", _result([["2025-01", 111111.0]]), disclosures=["Quarters are fiscal."]
    )
    first = review_answer("It was 111,111. Quarters are fiscal.", s)
    assert first.status == "pass"

    s.remember("run_sql", _result([["2025-02", 222222.0]]), disclosures=["Unverified ad hoc SQL."])
    bare = review_answer("It was 222,222.", s)
    assert bare.status == "missing_disclosures"
    assert bare.disclosures_missing == ["Unverified ad hoc SQL."], "not the first question's"
    assert review_answer("It was 222,222, from unverified ad hoc SQL.", s).status == "pass"


def test_a_draft_quoting_nothing_owes_what_no_passing_draft_has_covered():
    s = SessionStore().get("conn-none")
    s.remember("query_metrics", _result([["US", 600.0]]), disclosures=["Net of returns."])
    assert review_answer("The US led.", s).status == "missing_disclosures"
    assert review_answer("The US led, net of returns.", s).status == "pass"
    assert review_answer("The US led.", s).status == "pass", "covered now"


def test_review_without_results():
    s = SessionStore().get("conn-2")
    assert review_answer("Anything 123,456", s).status == "no_results_in_session"


def test_sessions_expire():
    store = SessionStore(ttl_s=0)
    store.get("a")
    store.get("b")
    assert len(store) == 1
