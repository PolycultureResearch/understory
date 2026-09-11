"""The traps matcher: run a registry against a metric spec and its question.

`check` is deterministic and cheap. It never resolves dates and never talks to
the warehouse; it only decides whether the spec can run as written, needs a
clarification, or must be refused, and it applies `prefer` policies and any
clarification choices the chatbot sent back.

Matching is a normalized whole-phrase match. The question text and every name
in the spec are lowercased, punctuation and underscores become spaces, and a
phrase matches only at word boundaries, so `revenue` matches "net revenue" but
not "revenues". Before a collision or dimension role is matched against the
question, the names and labels of its own candidates are masked out, so a
question that says "net revenue" does not trip the `revenue` collision. That is
the "covered question" flow in design section 13.

How applied choices are marked: a satisfied `ask` trap and an applied `prefer`
trap both appear in `TrapsOutcome.fired` and add a `Disclosure` whose `source`
is the trap id. They never appear in `TrapsOutcome.clarifications`.

Window ids: a `default_window` convention with policy `ask_if_absent` yields a
clarification of slot `convention` whose option ids are the keys of
`schema.WINDOWS`. When the chatbot resubmits with a choice for that trap, the
spec's time is left untouched and a disclosure names the window; the server
translates the window id into dates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from understory.traps.schema import (
    WINDOWS,
    CollisionTrap,
    ConventionTrap,
    DimensionRoleTrap,
    Registry,
    normalize,
)
from understory.types import (
    Catalog,
    Clarification,
    Disclosure,
    MetricSpec,
    Option,
    Refusal,
    TrapsOutcome,
)


def check(
    spec: MetricSpec,
    registry: Registry,
    catalog: Catalog,
    *,
    max_clarifications: int,
) -> TrapsOutcome:
    """Run every trap against the spec. Returns a modified copy of the spec."""
    spec = spec.model_copy(deep=True)
    question = normalize(spec.question) if spec.question else ""
    fired: list[str] = []
    clarifications: list[Clarification] = []
    disclosures: list[Disclosure] = []

    # Declared unanswerable wins over everything else.
    for trap in registry.unanswerable:
        phrase = _first_match(trap.phrase, question)
        if phrase is not None:
            fired.append(trap.id)
            return TrapsOutcome(
                spec=spec,
                refusal=Refusal(reason="unanswerable", message=trap.reason, phrase=phrase),
                fired=fired,
            )

    for collision in registry.collisions:
        _check_collision(collision, spec, question, catalog, fired, clarifications, disclosures)

    for role in registry.dimension_roles:
        _check_dimension_role(role, spec, question, catalog, fired, clarifications, disclosures)

    for convention in registry.conventions:
        _check_convention(convention, spec, fired, clarifications, disclosures)

    clarifications.sort(key=lambda c: (c.priority, c.trap))

    refusal: Refusal | None = None
    if len(clarifications) > max_clarifications:
        phrases = [c.phrase for c in clarifications]
        refusal = Refusal(
            reason="too_broad",
            message=(
                f"The question needs {len(clarifications)} clarifications "
                f"({', '.join(repr(p) for p in phrases)}) and the limit is "
                f"{max_clarifications}. Ask a narrower question."
            ),
            phrase=phrases[0],
            suggestions=phrases,
        )

    return TrapsOutcome(
        spec=spec,
        clarifications=clarifications,
        disclosures=disclosures,
        refusal=refusal,
        fired=fired,
    )


# --------------------------------------------------------------------------- #
# Text matching
# --------------------------------------------------------------------------- #


def _first_match(phrases: Iterable[str], text: str) -> str | None:
    """The first phrase that occurs in `text` as a whole word or phrase."""
    if not text:
        return None
    for phrase in phrases:
        needle = normalize(phrase)
        if needle and _contains(text, needle):
            return needle
    return None


def _contains(text: str, needle: str) -> bool:
    return re.search(rf"(?<!\S){re.escape(needle)}(?!\S)", text) is not None


def _mask(text: str, terms: Iterable[str]) -> str:
    """Blank out whole-phrase occurrences of each term, longest first."""
    for term in sorted({normalize(t) for t in terms if t}, key=len, reverse=True):
        if term:
            text = re.sub(rf"(?<!\S){re.escape(term)}(?!\S)", " ", text)
    return text


def _metric_terms(names: Iterable[str], catalog: Catalog) -> list[str]:
    terms: list[str] = []
    for name in names:
        terms.append(name)
        info = catalog.metrics.get(name)
        if info is not None and info.label:
            terms.append(info.label)
    return terms


def _dimension_terms(names: Iterable[str], catalog: Catalog) -> list[str]:
    terms: list[str] = []
    for name in names:
        terms.append(name)
        info = catalog.dimensions.get(name)
        if info is not None and info.label:
            terms.append(info.label)
    return terms


def _fires(
    phrases: list[str],
    question: str,
    spec_text: str,
    *,
    names_candidate: bool,
    candidate_terms: list[str],
) -> str | None:
    """The phrase that fired, or None.

    A phrase in the question fires unless it only occurs inside a candidate's
    own name or label. A phrase in the spec's names fires only when the spec
    does not already name one of the candidates explicitly.

    A candidate name that is itself one of the phrases (a metric called `mrr`
    behind the phrase `mrr`) is not masked, or the phrase could never fire.
    """
    phrase_set = {normalize(p) for p in phrases}
    maskable = [t for t in candidate_terms if normalize(t) not in phrase_set]
    phrase = _first_match(phrases, _mask(question, maskable))
    if phrase is not None:
        return phrase
    if names_candidate:
        return None
    return _first_match(phrases, spec_text)


def _choice_for(spec: MetricSpec, trap_id: str) -> str | None:
    """The chatbot's choice for a trap. Accepts the full id or the id without its kind."""
    short = trap_id.split(":", 1)[-1]
    for c in spec.clarifications:
        if c.trap in (trap_id, short):
            return c.choice
    return None


def _label(name: str, catalog: Catalog) -> str:
    info = catalog.metrics.get(name) or catalog.dimensions.get(name)
    return (info.label if info is not None and info.label else None) or name


def _description(name: str, catalog: Catalog) -> str | None:
    info = catalog.metrics.get(name) or catalog.dimensions.get(name)
    return info.description if info is not None else None


# --------------------------------------------------------------------------- #
# Collisions
# --------------------------------------------------------------------------- #


def _check_collision(
    trap: CollisionTrap,
    spec: MetricSpec,
    question: str,
    catalog: Catalog,
    fired: list[str],
    clarifications: list[Clarification],
    disclosures: list[Disclosure],
) -> None:
    candidate_terms = _metric_terms(trap.candidates, catalog)
    phrase = _fires(
        trap.phrase,
        question,
        normalize("  ".join(spec.metrics)),
        names_candidate=any(m in trap.candidates for m in spec.metrics),
        candidate_terms=candidate_terms,
    )
    if phrase is None:
        return
    fired.append(trap.id)

    if trap.is_ask:
        choice = _choice_for(spec, trap.id)
        if choice is None or choice not in trap.candidates:
            options = [
                Option(
                    id=c,
                    label=_label(c, catalog),
                    hint=trap.hint.get(c) or _description(c, catalog),
                )
                for c in trap.candidates
            ]
            clarifications.append(
                Clarification(
                    trap=trap.id,
                    slot="metric",
                    phrase=phrase,
                    options=options,
                    priority=trap.priority,
                )
            )
            return
        _apply_metric(spec, trap, choice, keep=set(), append=True)
        disclosures.append(
            Disclosure(
                text=f"'{phrase}' is read as {_label(choice, catalog)}, as chosen.",
                source=trap.id,
            )
        )
        return

    preferred = trap.preferred
    assert preferred is not None
    # A candidate the question names by its own name or label is left alone.
    explicit = {
        c
        for c in trap.candidates
        if c != preferred and _first_match(_metric_terms([c], catalog), question)
    }
    _apply_metric(spec, trap, preferred, keep=explicit, append=False)
    text = f"'{phrase}' is read as {_label(preferred, catalog)}."
    hint = trap.hint.get(preferred) or _description(preferred, catalog)
    if hint:
        text = f"{text} {hint}"
    disclosures.append(Disclosure(text=text, source=trap.id))


def _apply_metric(
    spec: MetricSpec, trap: CollisionTrap, chosen: str, *, keep: set[str], append: bool
) -> None:
    """Substitute `chosen` for the trap's other candidates in the spec's metrics.

    With `append`, a spec that names none of the candidates gets `chosen` added,
    because the user explicitly picked it.
    """
    new: list[str] = []
    for m in spec.metrics:
        if m in trap.candidates and m not in keep:
            m = chosen
        if m not in new:
            new.append(m)
    if append and not any(m in trap.candidates for m in new):
        new.append(chosen)
    spec.metrics = new


# --------------------------------------------------------------------------- #
# Dimension roles
# --------------------------------------------------------------------------- #


def _spec_dimensions(spec: MetricSpec) -> list[str]:
    return list(spec.group_by) + [w.dimension for w in spec.where]


def _check_dimension_role(
    trap: DimensionRoleTrap,
    spec: MetricSpec,
    question: str,
    catalog: Catalog,
    fired: list[str],
    clarifications: list[Clarification],
    disclosures: list[Disclosure],
) -> None:
    used = _spec_dimensions(spec)
    phrase = _fires(
        trap.phrase,
        question,
        normalize("  ".join(used)),
        names_candidate=any(d in trap.candidates for d in used),
        candidate_terms=_dimension_terms(trap.candidates, catalog),
    )
    if phrase is None:
        return
    fired.append(trap.id)

    if trap.is_ask:
        choice = _choice_for(spec, trap.id)
        if choice is None or choice not in trap.candidates:
            options = [
                Option(id=c, label=_label(c, catalog), hint=_description(c, catalog))
                for c in trap.candidates
            ]
            clarifications.append(
                Clarification(
                    trap=trap.id,
                    slot="dimension",
                    phrase=phrase,
                    options=options,
                    priority=trap.priority,
                )
            )
            return
        _apply_dimension(spec, trap, choice)
        text = trap.disclose or f"'{phrase}' is read as {_label(choice, catalog)}, as chosen."
        disclosures.append(Disclosure(text=text, source=trap.id))
        return

    preferred = trap.preferred
    assert preferred is not None
    _apply_dimension(spec, trap, preferred)
    text = trap.disclose or f"'{phrase}' is read as {_label(preferred, catalog)}."
    disclosures.append(Disclosure(text=text, source=trap.id))


def _apply_dimension(spec: MetricSpec, trap: DimensionRoleTrap, chosen: str) -> None:
    """Replace the trap's other candidates with `chosen` in group_by and where."""
    new: list[str] = []
    for d in spec.group_by:
        if d in trap.candidates:
            d = chosen
        if d not in new:
            new.append(d)
    spec.group_by = new
    for w in spec.where:
        if w.dimension in trap.candidates:
            w.dimension = chosen


# --------------------------------------------------------------------------- #
# Conventions
# --------------------------------------------------------------------------- #


def _check_convention(
    trap: ConventionTrap,
    spec: MetricSpec,
    fired: list[str],
    clarifications: list[Clarification],
    disclosures: list[Disclosure],
) -> None:
    if trap.name == "time_anchor":
        # The server anchors an omitted `end` to the latest available date.
        if spec.time.end is not None or not trap.wants_disclosure:
            return
        fired.append(trap.id)
        text = trap.disclose_text or (
            "The time window ends at the latest date with data, not today."
        )
        disclosures.append(Disclosure(text=text, source=trap.id))
        return

    if spec.time.start is not None or spec.time.end is not None:
        return

    if trap.policy == "ask_if_absent":
        fired.append(trap.id)
        choice = _choice_for(spec, trap.id)
        if trap.name == "default_window" and choice in WINDOWS:
            disclosures.append(
                Disclosure(text=f"Time window: {WINDOWS[choice]}, as chosen.", source=trap.id)
            )
            return
        if trap.name == "default_window":
            options = [Option(id=k, label=v) for k, v in WINDOWS.items()]
        else:
            options = [Option(id=trap.value, label=trap.value)]
        clarifications.append(
            Clarification(
                trap=trap.id,
                slot="convention",
                phrase=trap.name,
                options=options,
                priority=trap.priority,
            )
        )
        return

    if not trap.wants_disclosure:
        return
    fired.append(trap.id)
    if trap.name == "default_window":
        generated = f"No time window was given; using {WINDOWS.get(trap.value, trap.value)}."
    else:
        generated = f"{trap.name}: {trap.value}."
    disclosures.append(Disclosure(text=trap.disclose_text or generated, source=trap.id))
