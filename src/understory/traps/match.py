"""The traps matcher: run a registry against a metric spec and its question.

`check` is deterministic and cheap. It never resolves dates and never talks to
the warehouse; it only decides whether the spec can run as written, needs a
clarification, or must be refused, and it applies `prefer` policies and any
clarification choices the chatbot sent back.

A `prefer` disclosure teaches as it discloses: it says what the phrase was read
as, gives that candidate's hint, and names the alternatives so the user can ask
for them next time without a round trip.

Matching is a normalized whole-phrase match. The question text and every name
in the spec are lowercased, punctuation and underscores become spaces, and a
phrase matches only at word boundaries, so `revenue` matches "net revenue" but
not "revenues". Before a collision or dimension role is matched against the
question, the names and labels of its own candidates are masked out, so a
question that says "net revenue" does not trip the `revenue` collision. That is
the "covered question" flow in design section 13.

A phrase is also covered when the spec names a metric (or dimension) outside
the trap's candidates whose own name or label contains the phrase: "sales" in
a spec for Sales Cycle Days, "conversions" in a spec for Direct Conversions.
The chatbot has already read the phrase as that metric, and the trap's remedy,
swapping in or asking among its candidates, does not fit a metric the trap
does not govern. See `_fires`.

A prefer is bounded by the catalog. It does not apply when the swap would
leave the spec filtering or grouping by a dimension the preferred candidate
does not carry (POS dollars by retailer banner, where the preferred wholesale
revenue has no banner). The spec's own candidate stays and the disclosure says
why, naming the preferred alternative. The same holds for a dimension role
whose preferred dimension is not on the spec's metrics.

How applied choices are marked: a satisfied `ask` trap and an applied `prefer`
trap both appear in `TrapsOutcome.fired` and add a `Disclosure` whose `source`
is the trap id. They never appear in `TrapsOutcome.clarifications`. A prefer
whose phrase matched but whose spec names none of its candidates changed
nothing, so it neither fires nor discloses: the disclosure describes what
happened to the spec, never a swap that did not.

Windows: a `default_window` convention fires when the spec names no dates. The
spec's time is left untouched and a disclosure names the window; the server
translates the window id into dates. A window is never asked for.
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

    # Roles before collisions: a role settles which entity's dimension the spec
    # filters or groups by, and a collision then judges its preferred metric
    # against the settled dimensions ("sales in the West": the West becomes
    # order country first, and net revenue, which carries it, then applies).
    for role in registry.dimension_roles:
        _check_dimension_role(role, spec, question, catalog, fired, clarifications, disclosures)

    for collision in registry.collisions:
        _check_collision(collision, spec, question, catalog, fired, clarifications, disclosures)

    for convention in registry.conventions:
        _check_convention(convention, spec, fired, clarifications, disclosures)
    if any(d.source == "convention:default_window" for d in disclosures):
        # The window sentence already says it ends at the latest date with data.
        disclosures[:] = [d for d in disclosures if d.source != "convention:time_anchor"]

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
    *,
    candidate_terms: list[str],
    covering_terms: Iterable[str],
) -> str | None:
    """The phrase that fired, or None.

    A phrase in the question fires unless it only occurs inside a candidate's
    own name or label. A candidate name that is itself one of the phrases (a
    metric called `mrr` behind the phrase `mrr`) is not masked, or the phrase
    could never fire.

    `covering_terms` are the names and labels of what the spec names outside
    the candidates. A phrase that sits inside one of them as a whole word is
    covered and does not fire: "sales" is covered by a spec for Sales Cycle
    Days, "conversions" by one for Direct Conversions. The chatbot has read
    the phrase as a metric the trap does not govern, and the trap's remedy
    (swap in or ask among its candidates) would be wrong for it. This is a
    rule about the spec rather than the question, because the label rarely
    appears verbatim in the question ("sales cycle by month" for "Sales Cycle
    Days"), so masking catalog labels in the question would catch only the
    verbatim ones. Requiring the spec to name the covering metric keeps the
    rule narrow: the trap yields only to a resolution the chatbot made.

    The spec's own names are never matched on their own. A phrase inside a
    candidate's name is the explicit-naming case, and a phrase inside any
    other name is covered, so there is nothing left for the spec to fire.
    """
    phrase_set = {normalize(p) for p in phrases}
    maskable = [t for t in candidate_terms if normalize(t) not in phrase_set]
    masked = _mask(question, maskable)
    covering = {normalize(t) for t in covering_terms if t}
    for phrase in phrases:
        needle = normalize(phrase)
        if not needle or not _contains(masked, needle):
            continue
        if any(_contains(term, needle) for term in covering):
            continue
        return needle
    return None


_GRAINS = ("day", "week", "month", "quarter", "year")


def _carries(metric: str, dimension: str, catalog: Catalog) -> bool:
    """Whether `metric` can be grouped or filtered by `dimension`, per the catalog.

    Mirrors the server's spec validation: `metric_time` is always allowed and a
    grain suffix (`order__order_date__month`) is stripped first. A metric the
    catalog does not know is not judged.
    """
    info = catalog.metrics.get(metric)
    if info is None:
        return True
    base, sep, suffix = dimension.rpartition("__")
    if sep and base and suffix in _GRAINS:
        dimension = base
    return dimension == "metric_time" or dimension in info.dimensions


def _dimension_uses(spec: MetricSpec, names: Iterable[str], catalog: Catalog) -> str:
    """How the spec uses each dimension, for a disclosure: "the filter on Plan"."""
    parts: list[str] = []
    for d in names:
        how = "group by" if d in spec.group_by else "filter on"
        parts.append(f"the {how} {_label(d, catalog)}")
    return " and ".join(parts)


def _labels(names: Iterable[str], catalog: Catalog) -> str:
    return " and ".join(_label(n, catalog) for n in names)


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


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _alternatives(
    candidates: list[str],
    applied: str,
    explicit: set[str],
    catalog: Catalog,
    hints: dict[str, str],
) -> str:
    """The road not taken: " Ask for X (hint) or Y instead." Empty when there is none."""
    parts: list[str] = []
    for c in candidates:
        if c == applied or c in explicit:
            continue
        label = _label(c, catalog)
        hint = hints.get(c) or _description(c, catalog)
        parts.append(f"{label} ({hint.strip().rstrip('.')})" if hint else label)
    if not parts:
        return ""
    return " Ask for " + " or ".join(parts) + " instead."


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
        candidate_terms=candidate_terms,
        covering_terms=_metric_terms(
            [m for m in spec.metrics if m not in trap.candidates], catalog
        ),
    )
    if phrase is None:
        return

    if trap.is_ask:
        fired.append(trap.id)
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
    # So is one whose where or group_by the preferred candidate cannot carry.
    swapping = [m for m in spec.metrics if m in trap.candidates and m not in explicit | {preferred}]
    blocking = (
        [d for d in _spec_dimensions(spec) if not _carries(preferred, d, catalog)]
        if swapping
        else []
    )
    keep = explicit | (set(swapping) if blocking else set())
    _apply_metric(spec, trap, preferred, keep=keep, append=False)

    present = [m for m in spec.metrics if m in trap.candidates]
    if not present:
        return  # The spec names none of the candidates; nothing was read as anything.
    fired.append(trap.id)
    if preferred in present:
        text = f"'{phrase}' is read as {_label(preferred, catalog)}."
        hint = trap.hint.get(preferred) or _description(preferred, catalog)
        if hint:
            text = f"{text} {_sentence(hint)}"
        text += _alternatives(trap.candidates, preferred, explicit, catalog, trap.hint)
    elif blocking:
        text = (
            f"'{phrase}' is read as {_labels(present, catalog)} because of "
            f"{_dimension_uses(spec, blocking, catalog)}, which "
            f"{_label(preferred, catalog)} does not carry."
        )
    else:
        text = f"'{phrase}' is read as {_labels(present, catalog)}, as named."
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
        candidate_terms=_dimension_terms(trap.candidates, catalog),
        covering_terms=_dimension_terms([d for d in used if d not in trap.candidates], catalog),
    )
    if phrase is None:
        return

    if trap.is_ask:
        fired.append(trap.id)
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
    swapping = [d for d in used if d in trap.candidates and d != preferred]
    blocking = [m for m in spec.metrics if not _carries(m, preferred, catalog)] if swapping else []
    if not blocking:
        _apply_dimension(spec, trap, preferred)

    present = [d for d in _spec_dimensions(spec) if d in trap.candidates]
    if not present:
        return  # The spec uses none of the candidates; nothing was read as anything.
    fired.append(trap.id)
    if preferred in present:
        text = trap.disclose or (
            f"'{phrase}' is read as {_label(preferred, catalog)}."
            + _alternatives(trap.candidates, preferred, set(), catalog, {})
        )
    else:
        text = (
            f"'{phrase}' is read as {_labels(present, catalog)} because "
            f"{_labels(blocking, catalog)} does not carry {_label(preferred, catalog)}."
        )
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
    if not trap.wants_disclosure:
        return
    fired.append(trap.id)
    if trap.name == "default_window":
        label = WINDOWS.get(trap.value, trap.value).lower()
        generated = (
            f"No time window was given, so this covers the {label} ending at the latest "
            "date with data, not today. Name a period for a different window."
        )
    else:
        generated = f"{trap.name}: {trap.value}."
    disclosures.append(Disclosure(text=trap.disclose_text or generated, source=trap.id))
