"""The oracle replay: what the traps check would have done with the user's own words.

The traps matcher reads its phrases from `spec.question`, and the chatbot
writes that field. The server never sees what the user typed, so a trap that
did not fire because the model left the question out, tidied "top line" into
"gross revenue", or sent only the follow-up ("split that by country") leaves no
trace in the log. The harness is the one place that knows both sides: the spec
the model sent and the golden item's real question.

`replay_spec` runs `traps.match.check` twice on one sent spec, as sent and with
`question` replaced by the oracle question, and reports every trap that fired
only the second time. That is a bypass. Each one carries its effect, because
they are not equally bad:

- `refusal`: a declared unanswerable phrase was not refused.
- `ask`: a clarification the data owner signed off on was never returned.
- `swap`: a prefer would have changed the metric or dimension that ran.
- `disclosure`: the spec already named the preferred candidate, so only the
  sentence saying how the phrase was read went missing.

The first three are material: the number, or whether there is one, differs.

For a multi-turn item the oracle question is every user turn so far, joined,
since the trap word is often in the first turn and the query in the second.
That over-counts when a later turn takes the word back ("revenue last month",
then "no, gross revenue"): the bare word is still in the joined text. Write
such items so the bare word never appears, or read their bypasses by hand.

The replay always runs with the spec-keyed disclosures off, whatever the
tenant's switch says. It measures the phrase path; the spec-keyed rule is the
thing being tested against it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from understory.traps.match import check
from understory.traps.schema import Registry
from understory.types import Catalog, MetricSpec, TrapsOutcome

BypassEffect = Literal["refusal", "ask", "swap", "disclosure"]

MATERIAL: tuple[BypassEffect, ...] = ("refusal", "ask", "swap")
"""Effects that change the number or whether there is one."""


class Bypass(BaseModel):
    """One trap the user's words would have fired and the sent spec did not."""

    trap: str
    effect: BypassEffect
    turn: int = 1
    """Which user turn the query came from, 1-based."""
    sent_question: str | None = None
    """What the model put in `spec.question`. None when it sent nothing."""
    metrics: list[str] = Field(default_factory=list)
    """The metrics the sent spec ran, after the traps check it did get."""

    @property
    def material(self) -> bool:
        return self.effect in MATERIAL

    def __str__(self) -> str:
        return f"{self.trap} ({self.effect})"


def replay_spec(
    spec: MetricSpec | dict[str, Any],
    oracle_question: str,
    registry: Registry,
    catalog: Catalog,
    *,
    max_clarifications: int = 3,
    turn: int = 1,
) -> list[Bypass]:
    """Bypasses for one sent spec. A spec that does not validate has none."""
    if isinstance(spec, dict):
        try:
            spec = MetricSpec.model_validate(spec)
        except Exception:
            return []
    sent = check(spec, registry, catalog, max_clarifications=max_clarifications)
    oracle = check(
        spec.model_copy(update={"question": oracle_question}),
        registry,
        catalog,
        max_clarifications=max_clarifications,
    )
    out: list[Bypass] = []
    for trap_id in oracle.fired:
        if trap_id in sent.fired:
            continue
        out.append(
            Bypass(
                trap=trap_id,
                effect=_effect(trap_id, sent, oracle, registry),
                turn=turn,
                sent_question=spec.question,
                metrics=list(sent.spec.metrics),
            )
        )
    return out


def replay_turns(
    turns: list[tuple[str, list[dict[str, Any]]]],
    registry: Registry,
    catalog: Catalog,
    *,
    max_clarifications: int = 3,
) -> list[Bypass]:
    """Bypasses across a conversation.

    `turns` is, per user turn in order, the oracle question for that turn (the
    user's words so far) and the specs the model sent during it. A trap is
    reported once per turn however many queries in the turn missed it.
    """
    out: list[Bypass] = []
    for n, (oracle_question, specs) in enumerate(turns, start=1):
        seen: set[str] = set()
        for spec in specs:
            for b in replay_spec(
                spec,
                oracle_question,
                registry,
                catalog,
                max_clarifications=max_clarifications,
                turn=n,
            ):
                if b.trap not in seen:
                    seen.add(b.trap)
                    out.append(b)
    return out


def _effect(
    trap_id: str, sent: TrapsOutcome, oracle: TrapsOutcome, registry: Registry
) -> BypassEffect:
    if oracle.refusal is not None and oracle.refusal.reason == "unanswerable":
        return "refusal"
    if any(c.trap == trap_id for c in oracle.clarifications):
        return "ask"
    candidates = _candidates(trap_id, registry)
    if _named(sent.spec, candidates) != _named(oracle.spec, candidates):
        return "swap"
    return "disclosure"


def _candidates(trap_id: str, registry: Registry) -> set[str]:
    for trap in [*registry.collisions, *registry.dimension_roles]:
        if trap.id == trap_id:
            return set(trap.candidates)
    return set()


def _named(spec: MetricSpec, candidates: set[str]) -> list[str]:
    """The trap's candidates the spec names, as metrics, group_by or where."""
    names = [*spec.metrics, *spec.group_by, *(w.dimension for w in spec.where)]
    return sorted({n for n in names if n in candidates})


__all__ = ["MATERIAL", "Bypass", "BypassEffect", "replay_spec", "replay_turns"]
