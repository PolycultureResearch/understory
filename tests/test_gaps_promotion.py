"""Gap promotion: backlog rows become open golden items (design 10.4)."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from understory.cli import app
from understory.harness.gaps import BacklogRow, backlog_sql, draft_from_backlog, gap_id
from understory.harness.golden import load_golden, split_open
from understory.tenant import tenants_dir

ROWS = [
    BacklogRow(
        kind="ungoverned_sql",
        key="main_marts.fct_orders",
        occurrences=7,
        users=4,
        first_seen=datetime(2026, 9, 1, 10),
        last_seen=datetime(2026, 9, 20, 16),
        sample_question="What share of orders used a promo code last month?",
        sample_reason="No metric counts promo orders.",
        sample_sql=(
            "select count(*) filter (where promo_code is not null)\n  from main_marts.fct_orders"
        ),
    ),
    BacklogRow(
        kind="invalid",
        key="order__promo_code",
        occurrences=3,
        users=1,
        first_seen=datetime(2026, 9, 3),
        last_seen=datetime(2026, 9, 3),
        sample_question="Revenue by promo code in August",
        sample_nearest=["order__channel", "order__region"],
    ),
    BacklogRow(kind="unanswerable", key="profit", occurrences=1, users=1),
]


def test_each_row_becomes_an_open_item():
    items = draft_from_backlog(ROWS)
    assert [i.id for i in items] == [
        "gap_ungoverned_sql_main_marts_fct_orders",
        "gap_invalid_order__promo_code",
        "gap_unanswerable_profit",
    ]
    assert all(i.is_open and i.kind == "gap" for i in items)
    assert all(i.expected.status == "resolved" and i.expected.governed for i in items)

    sql, invalid, unanswerable = items
    assert sql.source == "gap:ungoverned_sql:main_marts.fct_orders"
    assert "Asked 7 times by 4 people between 2026-09-01 and 2026-09-20" in sql.notes
    assert "select count(*) filter (where promo_code is not null) from" in sql.notes
    assert "on 2026-09-03" in invalid.notes
    assert "Nearest in the catalog: order__channel, order__region." in invalid.notes
    assert unanswerable.question.startswith("TODO")
    assert "registry declares this unanswerable" in unanswerable.notes


def test_thresholds_and_limit():
    assert len(draft_from_backlog(ROWS, min_users=2)) == 1
    assert len(draft_from_backlog(ROWS, min_occurrences=3)) == 2
    assert len(draft_from_backlog(ROWS, limit=1)) == 1


def test_long_keys_get_a_stable_short_id():
    key = ",".join(f"analytics.fct_table_number_{n}" for n in range(6))
    first = gap_id("ungoverned_sql", key)
    assert first == gap_id("ungoverned_sql", key)
    assert len(first) <= len("gap_ungoverned_sql_") + 48
    assert first != gap_id("ungoverned_sql", key + ",x")


def test_backlog_sql_takes_only_a_relation_name():
    assert "from analytics.mart_semantic_backlog where tenant = 'o''neil'" in backlog_sql(
        "analytics.mart_semantic_backlog", "o'neil"
    )
    with pytest.raises(ValueError):
        backlog_sql("mart; drop table x", "alpenglow")


def test_open_gaps_are_split_from_the_items_to_score():
    closed = draft_from_backlog(ROWS[:1])[0].model_copy(update={"spec": {"metrics": ["orders"]}})
    scored, open_ = split_open([closed, *draft_from_backlog(ROWS[1:])])
    assert [i.id for i in scored] == [closed.id]
    assert len(open_) == 2


def _backlog_db(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(
        "create table mart_semantic_backlog (tenant varchar, kind varchar, key varchar, "
        "occurrences bigint, users bigint, first_seen timestamptz, last_seen timestamptz, "
        "sample_question varchar, sample_reason varchar, sample_sql varchar, "
        "sample_nearest varchar[])"
    )
    con.execute(
        "insert into mart_semantic_backlog values "
        "('alpenglow', 'invalid', 'order__promo_code', 3, 2, '2026-09-01', '2026-09-02', "
        " 'Revenue by promo code?', null, null, ['order__channel']), "
        "('alpenglow', 'unanswerable', 'profit', 5, 1, '2026-09-01', '2026-09-05', "
        " 'What was profit?', 'profit is not modeled', null, null), "
        "('white_cube', 'unanswerable', 'churn', 9, 9, '2026-09-01', '2026-09-05', "
        " 'Churn?', null, null, null)"
    )
    con.close()


def test_the_command_appends_once_and_leaves_the_rest(tmp_path):
    tenant = tmp_path / "alpenglow"
    shutil.copytree(tenants_dir() / "alpenglow", tenant)
    realistic = tenant / "golden" / "realistic.yml"
    before = load_golden(realistic)
    db = tmp_path / "backlog.duckdb"
    _backlog_db(db)
    args = ["promote-gaps", "--tenant", str(tenant), "--db", str(db)]

    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "2 gaps in the backlog, 2 past the thresholds" in first.output
    after = load_golden(realistic)
    assert after[: len(before)] == before
    added = after[len(before) :]
    assert [i.id for i in added] == ["gap_invalid_order__promo_code", "gap_unanswerable_profit"]
    assert all(i.is_open for i in added)

    again = CliRunner().invoke(app, args)
    assert again.exit_code == 0, again.output
    assert "0 new, 2 already there" in again.output
    assert len(load_golden(realistic)) == len(after)


def test_dry_run_writes_nothing(tmp_path):
    tenant = tmp_path / "alpenglow"
    shutil.copytree(tenants_dir() / "alpenglow", tenant)
    realistic = tenant / "golden" / "realistic.yml"
    text = realistic.read_text()
    db = tmp_path / "backlog.duckdb"
    _backlog_db(db)
    result = CliRunner().invoke(
        app,
        ["promote-gaps", "-t", str(tenant), "--db", str(db), "--min-users", "2", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "gap_invalid_order__promo_code" in result.output
    assert "gap_unanswerable_profit" not in result.output
    assert realistic.read_text() == text
