"""Per-connection session state and the log_answer number check.

The store is in memory only. It remembers the results each session has seen so
`log_answer` can check that every number in a draft traces to one of them, and
the disclosures the session owes. Nothing here is persisted; the telemetry
writer already logged the events. Sessions expire after inactivity.
"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from understory.types import AnswerReview, NumberCheck, Result

_NUMBER = re.compile(
    r"""
    (?<![\w.])                # not inside a word or a decimal
    [-+]?
    (?:\$|€|£)?               # currency prefix
    (\d{1,3}(?:,\d{3})+|\d+)  # integer part with optional thousands separators
    (\.\d+)?                  # decimal part
    \s?(%|k|m|bn|b|million|thousand|billion)?  # suffix
    (?!\w|\.\d)              # not followed by a word char or another decimal
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SUFFIX = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}

# Numbers that appear in prose without being data: years, small counts, list
# indexes. A draft saying "three regions" or "in 2025" should not fail.
_IGNORE_BELOW = 13
_YEAR_RANGE = (1990, 2100)


@dataclass
class StoredResult:
    result_id: str
    tool: str
    result: Result
    metrics: list[str] = field(default_factory=list)
    numeric_values: list[tuple[str, float]] = field(default_factory=list)
    """(column, value) for every numeric cell, flattened, for the check."""


@dataclass
class Session:
    key: str
    user_hash: str | None = None
    results: dict[str, StoredResult] = field(default_factory=dict)
    disclosures: list[str] = field(default_factory=list)
    last_seen: float = field(default_factory=time.monotonic)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def remember(self, tool: str, result: Result, metrics: list[str] | None = None) -> str:
        result_id = f"r_{uuid.uuid4().hex[:10]}"
        values: list[tuple[str, float]] = []
        for row in result.rows:
            for col, cell in zip(result.columns, row, strict=False):
                if isinstance(cell, bool):
                    continue
                if isinstance(cell, int | float) and math.isfinite(cell):
                    values.append((col.name, float(cell)))
        self.results[result_id] = StoredResult(result_id, tool, result, metrics or [], values)
        self.touch()
        return result_id

    def owe(self, disclosures: list[str]) -> None:
        for d in disclosures:
            if d not in self.disclosures:
                self.disclosures.append(d)


class SessionStore:
    def __init__(self, ttl_s: int = 3600) -> None:
        self._ttl = ttl_s
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, key: str, user_hash: str | None = None) -> Session:
        with self._lock:
            self._expire()
            s = self._sessions.get(key)
            if s is None:
                s = Session(key=key, user_hash=user_hash)
                self._sessions[key] = s
            elif user_hash and not s.user_hash:
                s.user_hash = user_hash
            s.touch()
            return s

    def _expire(self) -> None:
        now = time.monotonic()
        dead = [k for k, s in self._sessions.items() if now - s.last_seen > self._ttl]
        for k in dead:
            del self._sessions[k]

    def __len__(self) -> int:
        return len(self._sessions)


# --------------------------------------------------------------------------- #
# Number check
# --------------------------------------------------------------------------- #


_ISO_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?(?:T[\d:]+)?\b")


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """Every number in the draft as (literal, value). Percent stays as the shown value.

    ISO dates are removed first so "2025-03-31" does not yield 31 as a number
    that needs a source. Answers quote windows as dates all the time.
    """
    out: list[tuple[str, float]] = []
    text = _ISO_DATE.sub(" ", text)
    for m in _NUMBER.finditer(text):
        literal = m.group(0).strip()
        int_part = m.group(1).replace(",", "")
        dec = m.group(2) or ""
        try:
            value = float(int_part + dec)
        except ValueError:
            continue
        suffix = (m.group(3) or "").lower()
        if suffix in _SUFFIX:
            value *= _SUFFIX[suffix]
        out.append((literal, value))
    return out


def _is_prose_number(literal: str, value: float) -> bool:
    if literal.endswith("%"):
        return False
    if value < _IGNORE_BELOW and float(value).is_integer():
        return True
    looks_like_year = _YEAR_RANGE[0] <= value <= _YEAR_RANGE[1] and float(value).is_integer()
    return looks_like_year and "," not in literal


def _matches(shown: float, actual: float, literal: str) -> bool:
    """Does the shown number plausibly render `actual`?

    Accepts rounding to the precision shown, a half percent relative slack for
    prose rounding ("about 1.2M"), and percent rendering of a ratio.
    """
    candidates = [actual]
    if literal.endswith("%"):
        candidates.append(actual * 100)
    for a in candidates:
        if shown == a:
            return True
        decimals = len(literal.split(".")[1].rstrip("%kmbn ").strip()) if "." in literal else 0
        # Suffixed numbers ("1.2M") carry their precision in the suffix's scale.
        scale = 1.0
        for suf, mult in _SUFFIX.items():
            if literal.lower().rstrip().endswith(suf):
                scale = mult
                break
        tol = 0.5 * (10 ** (-decimals)) * scale
        if abs(shown - a) <= tol:
            return True
        if a != 0 and abs(shown - a) / abs(a) <= 0.005:
            return True
    return False


def review_answer(
    draft: str, session: Session, required_disclosures: list[str] | None = None
) -> AnswerReview:
    checks: list[NumberCheck] = []
    unsourced: list[NumberCheck] = []
    pool: list[tuple[str, str, float]] = [
        (r.result_id, col, v) for r in session.results.values() for col, v in r.numeric_values
    ]
    if not pool:
        return AnswerReview(status="no_results_in_session")

    for literal, value in extract_numbers(draft):
        if _is_prose_number(literal, value):
            continue
        hit = next(((rid, col) for rid, col, v in pool if _matches(value, v, literal)), None)
        check = NumberCheck(
            number=literal,
            value=value,
            sourced=hit is not None,
            result_id=hit[0] if hit else None,
            column=hit[1] if hit else None,
        )
        checks.append(check)
        if hit is None:
            unsourced.append(check)

    owed = required_disclosures if required_disclosures is not None else session.disclosures
    present = [d for d in owed if _disclosure_present(d, draft)]
    missing = [d for d in owed if d not in present]
    status = "pass" if not unsourced else "unsourced_numbers"
    return AnswerReview(
        status=status,
        checked=checks,
        unsourced=unsourced,
        disclosures_present=present,
        disclosures_missing=missing,
    )


def _disclosure_present(disclosure: str, draft: str) -> bool:
    """A disclosure counts as present when most of its content words appear in the draft."""
    words = [w for w in re.findall(r"[a-z0-9]+", disclosure.lower()) if len(w) > 3]
    if not words:
        return True
    d = draft.lower()
    hits = sum(1 for w in words if w in d)
    return hits / len(words) >= 0.6
