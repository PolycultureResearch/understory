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

`replay_conversation` adds what the off/on comparison of that rule needs:

- `ambiguous`: the user's words fired a collision or a dimension role on some
  spec the model sent. These items are the denominator.
- `wrong`: a `Reading` per non-preferred candidate that ran where the user's
  words would have swapped it or asked. Whether the reply then reported that
  query's number without naming the candidate, a silent wrong reading, is for
  the scorer, which has the reply.
- `spec_keyed`: every disclosure the spec-keyed rule issues for the specs as
  sent, whether or not the tenant has it on. With the switch off this is how
  often the rule would have spoken; with it on, the scorer checks which of
  them came back and which reached the reply.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from understory.traps.match import SPEC_SOURCE, check
from understory.traps.schema import CollisionTrap, DimensionRoleTrap, Registry
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


class Reading(BaseModel):
    """A non-preferred candidate that ran on a question the user left ambiguous."""

    trap: str
    candidate: str
    names: list[str] = Field(default_factory=list)
    """The candidate's name and label: what a reply would call it."""
    numbers: list[float] = Field(default_factory=list)
    """What the query returned, so the scorer can tell if the reply reported it."""
    turn: int = 1

    def __str__(self) -> str:
        return f"{self.trap}: {self.candidate}"


class SpecKeyed(BaseModel):
    """One disclosure the spec-keyed rule issues for a spec as the model sent it."""

    trap: str
    candidate: str
    text: str
    names: list[str] = Field(default_factory=list)
    """The candidate's name and label."""
    default_names: list[str] = Field(default_factory=list)
    """Names and labels of the reading the disclosure sets it against: the
    preferred candidate, or on an `ask` every other candidate."""
    turn: int = 1


class Replay(BaseModel):
    """Everything the replay found in one conversation. See the module docstring."""

    bypasses: list[Bypass] = Field(default_factory=list)
    ambiguous: bool = False
    wrong: list[Reading] = Field(default_factory=list)
    spec_keyed: list[SpecKeyed] = Field(default_factory=list)


class SentQuery(BaseModel):
    """One query_metrics call as the trace recorded it."""

    spec: dict[str, Any]
    numbers: list[float] = Field(default_factory=list)


def _outcomes(
    spec: MetricSpec | dict[str, Any],
    oracle_question: str,
    registry: Registry,
    catalog: Catalog,
    max_clarifications: int,
) -> tuple[MetricSpec, TrapsOutcome, TrapsOutcome] | None:
    """The spec, the check as sent, and the check with the user's words. None if invalid."""
    if isinstance(spec, dict):
        try:
            spec = MetricSpec.model_validate(spec)
        except Exception:
            return None
    sent = check(spec, registry, catalog, max_clarifications=max_clarifications)
    oracle = check(
        spec.model_copy(update={"question": oracle_question}),
        registry,
        catalog,
        max_clarifications=max_clarifications,
    )
    return spec, sent, oracle


def _bypasses(
    spec: MetricSpec, sent: TrapsOutcome, oracle: TrapsOutcome, registry: Registry, turn: int
) -> list[Bypass]:
    return [
        Bypass(
            trap=trap_id,
            effect=_effect(trap_id, sent, oracle, registry),
            turn=turn,
            sent_question=spec.question,
            metrics=list(sent.spec.metrics),
        )
        for trap_id in oracle.fired
        if trap_id not in sent.fired
    ]


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
    found = _outcomes(spec, oracle_question, registry, catalog, max_clarifications)
    if found is None:
        return []
    return _bypasses(*found, registry, turn)


def replay_conversation(
    turns: list[tuple[str, list[SentQuery]]],
    registry: Registry,
    catalog: Catalog,
    *,
    max_clarifications: int = 3,
) -> Replay:
    """The whole replay for one conversation.

    `turns` is, per user turn in order, the oracle question for that turn and
    the queries the model sent during it. Within a turn a trap, a reading and
    a spec-keyed disclosure are each reported once however many queries
    repeat them.
    """
    out = Replay()
    for n, (oracle_question, queries) in enumerate(turns, start=1):
        seen: set[tuple[str, str]] = set()
        for query in queries:
            found = _outcomes(query.spec, oracle_question, registry, catalog, max_clarifications)
            if found is None:
                continue
            spec, sent, oracle = found
            if any(_candidates(t, registry) for t in oracle.fired) or oracle.clarifications:
                out.ambiguous = True
            for b in _bypasses(spec, sent, oracle, registry, n):
                if ("bypass", b.trap) not in seen:
                    seen.add(("bypass", b.trap))
                    out.bypasses.append(b)
                if b.effect not in ("swap", "ask"):
                    continue
                trap = _trap(b.trap, registry)
                for name in _named(sent.spec, set(trap.candidates) - {trap.preferred}):
                    if ("wrong", name) in seen:
                        continue
                    seen.add(("wrong", name))
                    out.wrong.append(
                        Reading(
                            trap=b.trap,
                            candidate=name,
                            names=_names(name, catalog),
                            numbers=query.numbers,
                            turn=n,
                        )
                    )
            keyed = check(
                spec,
                registry,
                catalog,
                max_clarifications=max_clarifications,
                spec_disclosures=True,
            )
            for d in keyed.disclosures:
                if not d.source.startswith(SPEC_SOURCE) or d.candidate is None:
                    continue
                if ("keyed", d.text) in seen:
                    continue
                seen.add(("keyed", d.text))
                trap = _trap(d.source[len(SPEC_SOURCE) :], registry)
                defaults = (
                    [trap.preferred]
                    if trap.preferred
                    else [c for c in trap.candidates if c != d.candidate]
                )
                out.spec_keyed.append(
                    SpecKeyed(
                        trap=trap.id,
                        candidate=d.candidate,
                        text=d.text,
                        names=_names(d.candidate, catalog),
                        default_names=[n_ for c in defaults for n_ in _names(c, catalog)],
                        turn=n,
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
    """Bypasses across a conversation, from bare specs. See `replay_conversation`."""
    return replay_conversation(
        [(oracle, [SentQuery(spec=s) for s in specs]) for oracle, specs in turns],
        registry,
        catalog,
        max_clarifications=max_clarifications,
    ).bypasses


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


def _trap(trap_id: str, registry: Registry) -> CollisionTrap | DimensionRoleTrap:
    for trap in [*registry.collisions, *registry.dimension_roles]:
        if trap.id == trap_id:
            return trap
    raise KeyError(trap_id)


def _candidates(trap_id: str, registry: Registry) -> set[str]:
    """The candidates of a collision or dimension role. Empty for any other trap."""
    try:
        return set(_trap(trap_id, registry).candidates)
    except KeyError:
        return set()


def _names(name: str, catalog: Catalog) -> list[str]:
    """A metric's or dimension's name and its label, when it has one."""
    info = catalog.metrics.get(name) or catalog.dimensions.get(name)
    label = info.label if info is not None else None
    return [name, label] if label else [name]


def _named(spec: MetricSpec, candidates: set[str]) -> list[str]:
    """The trap's candidates the spec names, as metrics, group_by or where."""
    names = [*spec.metrics, *spec.group_by, *(w.dimension for w in spec.where)]
    return sorted({n for n in names if n in candidates})


__all__ = [
    "MATERIAL",
    "Bypass",
    "BypassEffect",
    "Reading",
    "Replay",
    "SentQuery",
    "SpecKeyed",
    "replay_conversation",
    "replay_spec",
    "replay_turns",
]
