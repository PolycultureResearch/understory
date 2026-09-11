"""Telemetry writer, hashing, and the dbt_understory package over its output."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from understory.telemetry import (
    EVENTS_SCHEMA,
    TEXT_SCHEMA,
    AnswerLogged,
    ClarificationApplied,
    ClarificationReturned,
    QueryExecuted,
    Refused,
    TelemetryWriter,
    TextRecord,
    ToolCalled,
    user_hash,
)
from understory.tenant import LogConfig

REPO = Path(__file__).resolve().parents[1]
DBT_PROJECT = REPO / "dbt_understory"
TEXT_ONLY_COLUMNS = {"question", "sql", "draft_answer", "spec_json"}


def _config(root: Path, tenant: str = "alpenglow", enabled: bool = True) -> LogConfig:
    return LogConfig(
        events_prefix=str(root / tenant / "events"),
        text_prefix=str(root / tenant / "text"),
        user_hash_secret="test-secret",
        enabled=enabled,
    )


def _parquet_files(prefix: Path) -> list[Path]:
    return sorted(prefix.glob("dt=*/*.parquet"))


def _wait_for(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #


def test_user_hash_is_stable_and_secret_bound():
    a = user_hash("alice@example.com", "secret-one")
    assert a == user_hash("alice@example.com", "secret-one")
    assert len(a) == 32
    assert int(a, 16) >= 0
    assert a != user_hash("alice@example.com", "secret-two")
    assert a != user_hash("bob@example.com", "secret-one")


# --------------------------------------------------------------------------- #
# writer
# --------------------------------------------------------------------------- #


def test_writer_writes_partitioned_parquet_per_family(tmp_path: Path):
    cfg = _config(tmp_path)
    uh = user_hash("alice", cfg.user_hash_secret)
    fixed = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
    writer = TelemetryWriter(cfg, "alpenglow", flush_interval_s=3600, clock=lambda: fixed)

    q = QueryExecuted(
        user_hash=uh,
        session_id="s1",
        metrics=["net_revenue"],
        dimensions=["order__country"],
        sql_hash="abc123",
        spec_hash="def456",
        row_count=12,
        governed=True,
        cache_hit=False,
        truncated=False,
        latency_ms=250,
    )
    events = [
        ToolCalled(
            user_hash=uh, session_id="s1", tool="get_context", status="resolved", latency_ms=5
        ),
        q,
        ClarificationReturned(
            user_hash=uh, session_id="s1", trap_id="revenue", options=["net", "gross"]
        ),
        ClarificationApplied(user_hash=uh, session_id="s1", trap_id="revenue", choice="net"),
        Refused(user_hash=uh, session_id="s1", reason="unanswerable", phrase="profit"),
        AnswerLogged(user_hash=uh, session_id="s1", numbers_checked=3, numbers_unsourced=1),
    ]
    for ev in events:
        writer.emit(ev)
    writer.emit_text(
        TextRecord(
            event_id=q.event_id,
            question="Net revenue by country?",
            spec_json='{"metrics": ["net_revenue"]}',
            sql="select 1",
        )
    )
    assert writer.pending == (6, 1)
    writer.close()
    writer.close()  # idempotent
    assert writer.pending == (0, 0)

    event_files = _parquet_files(tmp_path / "alpenglow" / "events")
    text_files = _parquet_files(tmp_path / "alpenglow" / "text")
    assert len(event_files) == 1 and len(text_files) == 1
    assert event_files[0].parent.name == "dt=2026-03-04"
    assert event_files[0].name.startswith("20260304T120000000000Z-")
    assert not list((tmp_path / "alpenglow" / "events").rglob(".tmp-*"))

    ev_table = pq.read_table(event_files[0])
    assert ev_table.schema.equals(EVENTS_SCHEMA, check_metadata=False)
    assert ev_table.num_rows == 6
    assert not TEXT_ONLY_COLUMNS & set(ev_table.column_names)
    rows = ev_table.to_pylist()
    assert [r["event"] for r in rows] == [
        "tool_called",
        "query_executed",
        "clarification_returned",
        "clarification_applied",
        "refused",
        "answer_logged",
    ]
    assert all(r["tenant"] == "alpenglow" for r in rows)
    assert rows[0]["tool"] == "get_context" and rows[0]["status"] == "resolved"
    assert rows[1]["governed"] is True and rows[1]["latency_ms"] == 250
    assert rows[2]["trap_id"] == "revenue"
    assert rows[4]["reason"] == "unanswerable"
    assert '"sql_hash":"abc123"' in rows[1]["payload"]
    assert "select 1" not in "".join(r["payload"] for r in rows)

    tx_table = pq.read_table(text_files[0])
    assert tx_table.schema.equals(TEXT_SCHEMA, check_metadata=False)
    tx = tx_table.to_pylist()
    assert len(tx) == 1
    assert tx[0]["event_id"] == q.event_id
    assert tx[0]["question"] == "Net revenue by country?"
    assert tx[0]["draft_answer"] is None

    # DuckDB reads the same layout with hive partitioning.
    con = duckdb.connect()
    glob = str(tmp_path / "*" / "events" / "*" / "*.parquet")
    got = con.execute(
        f"select event, dt, json_extract_string(payload, '$.sql_hash') "
        f"from read_parquet('{glob}', hive_partitioning=true) where event = 'query_executed'"
    ).fetchall()
    assert got == [("query_executed", datetime(2026, 3, 4).date(), "abc123")]


def test_disabled_writer_writes_nothing(tmp_path: Path):
    cfg = _config(tmp_path, enabled=False)
    writer = TelemetryWriter(cfg, "alpenglow", flush_interval_s=0.01)
    writer.emit(
        ToolCalled(user_hash="u", session_id="s", tool="t", status="resolved", latency_ms=1)
    )
    writer.emit_text(TextRecord(event_id="x", question="secret"))
    writer.flush()
    writer.close()
    assert writer.pending == (0, 0)
    assert not (tmp_path / "alpenglow").exists()


def test_background_flush_on_buffer_size(tmp_path: Path):
    cfg = _config(tmp_path)
    writer = TelemetryWriter(cfg, "alpenglow", flush_interval_s=3600, max_buffer=2)
    try:
        writer.emit(ToolCalled(user_hash="u", session_id="s", tool="t", status="ok", latency_ms=1))
        assert _parquet_files(tmp_path / "alpenglow" / "events") == []
        writer.emit(ToolCalled(user_hash="u", session_id="s", tool="t", status="ok", latency_ms=2))
        assert _wait_for(lambda: len(_parquet_files(tmp_path / "alpenglow" / "events")) == 1)
    finally:
        writer.close()


def test_background_flush_on_interval(tmp_path: Path):
    cfg = _config(tmp_path)
    writer = TelemetryWriter(cfg, "alpenglow", flush_interval_s=0.1)
    try:
        writer.emit_text(TextRecord(event_id="e1", question="q"))
        assert _wait_for(lambda: len(_parquet_files(tmp_path / "alpenglow" / "text")) == 1)
    finally:
        writer.close()


async def test_emit_from_async_code(tmp_path: Path):
    cfg = _config(tmp_path)
    with TelemetryWriter(cfg, "alpenglow", flush_interval_s=3600) as writer:
        writer.emit(ToolCalled(user_hash="u", session_id="s", tool="t", status="ok", latency_ms=1))
    assert len(_parquet_files(tmp_path / "alpenglow" / "events")) == 1


def test_write_failure_is_logged_not_raised(tmp_path: Path, caplog):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    cfg = LogConfig(
        events_prefix=str(blocker / "events"),
        text_prefix=str(blocker / "text"),
        user_hash_secret="s",
    )
    writer = TelemetryWriter(cfg, "alpenglow", flush_interval_s=3600)
    writer.emit(ToolCalled(user_hash="u", session_id="s", tool="t", status="ok", latency_ms=1))
    with caplog.at_level(logging.ERROR, logger="understory.telemetry.writer"):
        writer.close()
    assert any("dropping 1 events rows" in r.getMessage() for r in caplog.records)
    assert writer.pending == (0, 0)


def test_gcs_prefix_uploads_through_mocked_client(tmp_path: Path, monkeypatch):
    uploads: list[tuple[str, str, bytes]] = []

    class FakeBlob:
        def __init__(self, bucket: str, name: str) -> None:
            self.bucket, self.name = bucket, name

        def upload_from_filename(self, filename: str, content_type: str | None = None) -> None:
            uploads.append((self.bucket, self.name, Path(filename).read_bytes()))

    class FakeBucket:
        def __init__(self, name: str) -> None:
            self.name = name

        def blob(self, name: str) -> FakeBlob:
            return FakeBlob(self.name, name)

    class FakeClient:
        instances = 0

        def __init__(self) -> None:
            FakeClient.instances += 1

        def bucket(self, name: str) -> FakeBucket:
            return FakeBucket(name)

    fake = types.ModuleType("google.cloud.storage")
    fake.Client = FakeClient
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake)

    cfg = LogConfig(
        events_prefix="gs://understory-log/alpenglow/events",
        text_prefix="gs://understory-log/alpenglow/text/",
        user_hash_secret="s",
    )
    fixed = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
    writer = TelemetryWriter(cfg, "alpenglow", flush_interval_s=3600, clock=lambda: fixed)
    writer.emit(ToolCalled(user_hash="u", session_id="s", tool="t", status="ok", latency_ms=1))
    writer.emit_text(TextRecord(event_id="e", question="q"))
    writer.close()

    assert len(uploads) == 2
    names = sorted(name for _, name, _ in uploads)
    assert names[0].startswith("alpenglow/events/dt=2026-03-04/20260304T120000000000Z-")
    assert names[1].startswith("alpenglow/text/dt=2026-03-04/")
    assert {b for b, _, _ in uploads} == {"understory-log"}
    for _, name, payload in uploads:
        tmp = tmp_path / Path(name).name
        tmp.write_bytes(payload)
        assert pq.read_table(tmp).num_rows == 1
    assert FakeClient.instances == 2  # one lazily built client per sink


# --------------------------------------------------------------------------- #
# dbt package
# --------------------------------------------------------------------------- #


def _synthetic_log(root: Path, tenant: str = "alpenglow") -> dict[str, int]:
    """Two users, several sessions, every event type. Returns expected counts."""
    cfg = _config(root, tenant)
    alice = user_hash("alice", cfg.user_hash_secret)
    bob = user_hash("bob", cfg.user_hash_secret)
    t0 = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
    writer = TelemetryWriter(cfg, tenant, flush_interval_s=3600, clock=lambda: t0)
    counts = {"questions": 0, "refusals": 0, "clarifications": 0, "abandoned": 0, "ungoverned": 0}

    def question(uh: str, sid: str, ts: datetime, governed: bool, text: str) -> None:
        ev = QueryExecuted(
            user_hash=uh,
            session_id=sid,
            ts=ts,
            metrics=["net_revenue"],
            dimensions=["order__country"],
            sql_hash="h",
            spec_hash="s",
            row_count=5,
            governed=governed,
            latency_ms=100,
        )
        writer.emit(
            ToolCalled(
                user_hash=uh,
                session_id=sid,
                ts=ts,
                tool="query_metrics",
                status="resolved",
                latency_ms=110,
            )
        )
        writer.emit(ev)
        writer.emit_text(TextRecord(event_id=ev.event_id, ts=ts, question=text, sql="select 1"))
        counts["questions"] += 1
        counts["ungoverned"] += 0 if governed else 1

    def refusal(
        uh: str, sid: str, ts: datetime, reason: str, phrase: str | None, text: str
    ) -> None:
        ev = Refused(user_hash=uh, session_id=sid, ts=ts, reason=reason, phrase=phrase)
        writer.emit(ev)
        writer.emit_text(TextRecord(event_id=ev.event_id, ts=ts, question=text))
        counts["questions"] += 1
        counts["refusals"] += 1

    # Session A (alice): clarification answered, governed question, log_answer.
    a1 = t0
    writer.emit(
        ToolCalled(
            user_hash=alice,
            session_id="a1",
            ts=a1,
            tool="get_context",
            status="resolved",
            latency_ms=3,
        )
    )
    writer.emit(
        ClarificationReturned(
            user_hash=alice,
            session_id="a1",
            ts=a1 + timedelta(seconds=5),
            trap_id="revenue",
            options=["net", "gross"],
        )
    )
    writer.emit(
        ClarificationApplied(
            user_hash=alice,
            session_id="a1",
            ts=a1 + timedelta(seconds=40),
            trap_id="revenue",
            choice="net",
        )
    )
    counts["clarifications"] += 1
    question(
        alice, "a1", a1 + timedelta(seconds=41), True, "How were sales in the West last quarter?"
    )
    writer.emit(
        AnswerLogged(
            user_hash=alice,
            session_id="a1",
            ts=a1 + timedelta(seconds=60),
            numbers_checked=2,
            disclosures_present=1,
        )
    )

    # Session B (alice, two hours later): abandoned clarification and a refusal.
    b1 = t0 + timedelta(hours=2)
    writer.emit(
        ClarificationReturned(
            user_hash=alice, session_id="a2", ts=b1, trap_id="revenue", options=["net", "gross"]
        )
    )
    counts["clarifications"] += 1
    counts["abandoned"] += 1
    refusal(
        alice,
        "a2",
        b1 + timedelta(seconds=30),
        "unanswerable",
        "profit",
        "What is our profit by SKU?",
    )

    # Session C (bob, next day): run_sql fallback and an invalid refusal.
    c1 = t0 + timedelta(days=1)
    refusal(bob, "b1", c1, "invalid", None, "Return rate for promo orders?")
    question(bob, "b1", c1 + timedelta(seconds=20), False, "Return rate for promo orders?")
    question(bob, "b1", c1 + timedelta(minutes=5), True, "Net revenue by month")

    writer.close()
    counts["sessions"] = 3
    return counts


def test_dbt_build_models_the_log(tmp_path: Path):
    log_dir = tmp_path / "log"
    expected = _synthetic_log(log_dir)
    db = tmp_path / "understory.duckdb"
    env = {**os.environ, "UNDERSTORY_DBT_DB": str(db)}
    cmd = [
        "dbt",
        "build",
        "--project-dir",
        str(DBT_PROJECT),
        "--profiles-dir",
        str(DBT_PROJECT),
        "--target-path",
        str(tmp_path / "target"),
        "--log-path",
        str(tmp_path / "logs"),
        "--no-partial-parse",
        "--vars",
        f'{{"understory_log_dir": "{log_dir}"}}',
    ]
    proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]

    con = duckdb.connect(str(db), read_only=True)

    def one(sql: str):
        return con.execute(sql).fetchone()

    assert one("select count(*) from main.fct_questions")[0] == expected["questions"]
    assert one("select count(*) from main.fct_refusals")[0] == expected["refusals"]
    assert one("select count(*) from main.fct_questions where not governed")[0] == (
        expected["refusals"] + expected["ungoverned"]
    )
    assert one(
        "select count(*), sum(case when abandoned then 1 else 0 end) from main.fct_clarifications"
    ) == (
        expected["clarifications"],
        expected["abandoned"],
    )
    assert one("select count(*) from main.fct_sessions")[0] == expected["sessions"]
    assert one("select count(*) from main.fct_sessions where log_answer_called")[0] == 1
    sid = one("select session_id from main.fct_sessions where log_answer_called")[0]
    assert sid == "a1"

    daily = con.execute(
        "select day, questions, governed_share, clarification_rate, abandonment_rate, "
        "refusal_rate, sessions, log_answer_rate from main.mart_eval_daily order by day"
    ).fetchall()
    assert len(daily) == 2
    day1, day2 = daily
    assert day1[1] == 2 and day1[2] == 0.5 and day1[3] == 1.0 and day1[4] == 0.5
    assert day1[5] == 0.5 and day1[6] == 2 and day1[7] == 0.5
    assert day2[1] == 3 and day2[6] == 1 and day2[7] == 0.0

    backlog = con.execute(
        "select kind, phrase, occurrences from restricted.mart_semantic_backlog "
        "order by kind, phrase"
    ).fetchall()
    assert ("refused", "profit", 1) in backlog
    assert ("refused", "return rate for promo orders?", 1) in backlog
    assert ("ungoverned_sql", "return rate for promo orders?", 1) in backlog

    # The events-only side of the warehouse never sees text.
    cols = {r[0] for r in con.execute("describe main.fct_questions").fetchall()}
    assert not cols & TEXT_ONLY_COLUMNS
