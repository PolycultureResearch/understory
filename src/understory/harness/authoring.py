"""Drafting a tenant's trap set from its catalog and traps registry, and marking
snapshots verified.

Design 10.3 lists the sources of a client's golden sets, cheapest first. The
first two need no one's time: the catalog gives one canonical question per
metric and one per metric-by-dimension, and the traps registry gives one item
per `ask`, one disclosure check per `prefer`, one refusal per `unanswerable`
and one per convention. `draft_golden` writes those as template items with
correct specs and no numbers; `understory fill` snapshots the numbers; a person
rewrites the wording and drops what is dull.

The third source, the data owner, is a conversation, not a command: the
`golden-interview` skill runs it and turns the transcript into items in the
same shape. The fourth, gaps, is step 7.

`set_verified` is the approval step. A snapshot guards regression, not truth,
until a human has checked it against a known report; the flag records that
they did, and the deterministic eval carries it on to every later run.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from understory.harness.golden import GoldenItem, GoldenKind
from understory.harness.realistic import (
    _label,
    _month_end,
    _month_name,
    _month_start,
    _slug,
    _slug_month,
    dump_items,
)
from understory.traps.match import check as check_traps
from understory.traps.schema import Registry, _ChoiceTrap
from understory.types import Catalog, MetricInfo

# --------------------------------------------------------------------------- #
# Drafting
# --------------------------------------------------------------------------- #


def draft_golden(
    catalog: Catalog,
    registry: Registry,
    *,
    window: tuple[date, date],
    by_dimension: bool = True,
) -> list[GoldenItem]:
    """Template items from the catalog and the traps registry. No numbers.

    Every item asks about the last full month of the data window, so the
    numbers a later `fill` records do not move when the data grows. Catalog
    items carry `kind: catalog`; registry items carry `kind: trap`.

    Each template is then run through the traps check with no warehouse, and
    one the registry would stop is drafted as the ask it will get: a metric
    whose own name is an `ask` phrase (white_cube's `revenue`) can never be
    asked about without the question, so its canonical item expects the
    clarification and answers it with the metric itself.
    """
    month = last_full_month(window)
    items: list[GoldenItem] = []
    items.extend(_catalog_items(catalog, month, by_dimension=by_dimension))
    items.extend(_registry_items(catalog, registry, month))
    return [_reconcile(item, catalog, registry) for item in items]


def last_full_month(window: tuple[date, date]) -> date:
    """First day of the last month the window covers in full."""
    _, hi = window
    start = _month_start(hi)
    if _month_end(start) == hi:
        return start
    return _month_start(start - timedelta(days=1))


def _catalog_items(catalog: Catalog, month: date, *, by_dimension: bool) -> list[GoldenItem]:
    """One single-month item per metric, and one grouped by its first categorical dimension.

    The question names the metric by its label, which the traps check masks
    before matching phrases, so these resolve without a trap firing (design 5).
    """
    out: list[GoldenItem] = []
    when = f"{_month_name(month)} {month.year}"
    for name, metric in catalog.metrics.items():
        if metric.type == "cumulative":
            # A cumulative metric over a trailing window only resolves as a
            # series: MetricFlow needs metric_time in the group-by.
            out.append(
                _item(
                    id=f"{name}_by_week_{_slug_month(month)}",
                    question=f"What was {_label(name, catalog)} by week in {when}?",
                    kind="catalog",
                    source=f"catalog:{name}",
                    spec={
                        "metrics": [name],
                        "group_by": ["metric_time"],
                        "time": {"grain": "week", "start": month, "end": _month_end(month)},
                    },
                )
            )
            continue
        out.append(
            _item(
                id=f"{name}_{_slug_month(month)}",
                question=f"What was {_label(name, catalog)} in {when}?",
                kind="catalog",
                source=f"catalog:{name}",
                spec={"metrics": [name], "time": {"start": month, "end": _month_end(month)}},
            )
        )
        dim = _first_categorical(metric, catalog) if by_dimension else None
        if dim is not None:
            out.append(
                _item(
                    id=f"{name}_by_{_short(dim)}_{_slug_month(month)}",
                    question=(
                        f"{_label(name, catalog).capitalize()} by {_dim_words(dim)} for {when}"
                    ),
                    kind="catalog",
                    source=f"catalog:{name}",
                    spec={
                        "metrics": [name],
                        "group_by": [dim],
                        "time": {"start": month, "end": _month_end(month)},
                    },
                )
            )
    return out


def _registry_items(catalog: Catalog, registry: Registry, month: date) -> list[GoldenItem]:
    out: list[GoldenItem] = []
    when = f"{_month_name(month)} {month.year}"
    stamp = _slug_month(month)
    anchor = _first_metric(catalog)

    for trap in registry.collisions:
        phrase = _uncovered_phrase(trap, catalog)
        named = _non_preferred(trap)
        out.append(
            _choice_item(
                id=f"{_slug(phrase)}_{'ask' if trap.is_ask else 'prefer'}_{stamp}",
                question=f"How did {phrase} look in {when}?",
                trap=trap,
                catalog=catalog,
                spec={"metrics": [named], "time": {"start": month, "end": _month_end(month)}},
            )
        )

    for trap in registry.dimension_roles:
        phrase = _uncovered_phrase(trap, catalog)
        # A metric that carries the preferred candidate and another one, so a
        # prefer can actually swap. When none does, the prefer is bounded by
        # the catalog and discloses why the named dimension stayed instead.
        named, swappable = _swap_pair(trap, catalog)
        metric = swappable or _metric_with(catalog, [named]) or anchor
        if metric is None:
            continue
        out.append(
            _choice_item(
                id=f"{_slug(phrase)}_{'ask' if trap.is_ask else 'prefer'}_{stamp}",
                question=f"{_label(metric, catalog).capitalize()} by {phrase} for {when}",
                trap=trap,
                catalog=catalog,
                spec={
                    "metrics": [metric],
                    "group_by": [named],
                    "time": {"start": month, "end": _month_end(month)},
                },
                bounded=swappable is None,
            )
        )

    for trap in registry.unanswerable:
        phrase = trap.phrase[0]
        metric = anchor
        if metric is None:
            continue
        out.append(
            _item(
                id=f"{_slug(phrase)}_unanswerable",
                question=f"What is our {phrase}?",
                kind="trap",
                source=f"trap:{trap.id}",
                spec={"metrics": [metric]},
                status="unanswerable",
                disclosures=[_first_sentence(trap.reason)],
            )
        )

    for trap in registry.conventions:
        metric = anchor
        if metric is None:
            continue
        if trap.name == "default_window":
            out.append(
                _item(
                    id=f"{metric}_no_window",
                    question=f"How is {_label(metric, catalog)} doing?",
                    kind="trap",
                    source=f"trap:{trap.id}",
                    spec={"metrics": [metric]},
                    disclosures=[trap.value.replace("_", " ")],
                )
            )
        elif trap.name == "time_anchor":
            out.append(
                _item(
                    id=f"{metric}_open_end",
                    question=f"What has {_label(metric, catalog)} been since {when}?",
                    kind="trap",
                    source=f"trap:{trap.id}",
                    spec={"metrics": [metric], "time": {"start": month}},
                    disclosures=["anchored"],
                )
            )
    return out


def _choice_item(
    *,
    id: str,
    question: str,
    trap: _ChoiceTrap,
    catalog: Catalog,
    spec: dict,
    bounded: bool = False,
) -> GoldenItem:
    """An item for a collision or dimension role: an ask stops, a prefer discloses.

    `bounded` says the spec's metric cannot carry the preferred candidate, so
    the prefer will keep what was named and say why.
    """
    if trap.is_ask:
        choice = trap.candidates[0]
        return _item(
            id=id,
            question=question,
            kind="trap",
            source=f"trap:{trap.id}",
            spec=spec,
            status="needs_clarification",
            clarification_traps=[trap.id],
            answers={trap.id: choice},
            metrics=[choice] if choice in catalog.metrics else [],
        )
    preferred = trap.preferred or trap.candidates[0]
    # An applied prefer discloses the trap's own text when it declares one,
    # and names the preferred candidate otherwise.
    disclose = getattr(trap, "disclose", None)
    disclosures = ["does not carry"] if bounded else [disclose or _trap_label(preferred, catalog)]
    return _item(
        id=id,
        question=question,
        kind="trap",
        source=f"trap:{trap.id}",
        spec=spec,
        metrics=[preferred] if preferred in catalog.metrics else [],
        disclosures=disclosures,
    )


def _item(
    *,
    id: str,
    question: str,
    kind: GoldenKind,
    source: str,
    spec: dict,
    status: str = "resolved",
    clarification_traps: list[str] | None = None,
    answers: dict[str, str] | None = None,
    metrics: list[str] | None = None,
    disclosures: list[str] | None = None,
) -> GoldenItem:
    expected = {
        "status": status,
        "clarification_traps": clarification_traps or [],
        "answers": answers or {},
        "metrics": metrics or [],
        "disclosures": disclosures or [],
    }
    return GoldenItem(
        id=id, question=question, kind=kind, source=source, spec=spec, expected=expected
    )


def _reconcile(item: GoldenItem, catalog: Catalog, registry: Registry) -> GoldenItem:
    """Draft what the registry will do with the question, not what was hoped.

    A template expected to resolve that the traps check would stop becomes a
    `needs_clarification` item answered with the candidate the spec already
    names, or the first option when the spec names none of them.
    """
    if item.expected.status != "resolved":
        return item
    outcome = check_traps(item.metric_spec(), registry, catalog, max_clarifications=3)
    if not outcome.clarifications:
        return item
    named = set(item.spec.get("metrics", [])) | set(item.spec.get("group_by", []))
    answers: dict[str, str] = {}
    for c in outcome.clarifications:
        ids = [o.id for o in c.options]
        answers[c.trap] = next((i for i in ids if i in named), ids[0])
    exp = item.expected.model_copy(
        update={
            "status": "needs_clarification",
            "clarification_traps": list(answers),
            "answers": answers,
            "metrics": [a for a in answers.values() if a in catalog.metrics]
            or item.expected.metrics,
        }
    )
    asked = ", ".join(f"'{c.phrase}'" for c in outcome.clarifications)
    note = f"The registry asks on {asked}, so this item expects the ask and answers it."
    return item.model_copy(update={"expected": exp, "notes": note})


def _uncovered_phrase(trap: _ChoiceTrap, catalog: Catalog) -> str:
    """The trap's first phrase that no candidate's name or label contains.

    A phrase inside the label of the metric the spec names is masked before
    matching, so "revenue" against gross_revenue never fires; "sales" does.
    """
    labels = [(_label(c, catalog) + " " + c.replace("_", " ")).lower() for c in trap.candidates]
    for phrase in trap.phrase:
        if not any(phrase.lower() in label for label in labels):
            return phrase
    return trap.phrase[0]


def _swap_pair(trap: _ChoiceTrap, catalog: Catalog) -> tuple[str, str | None]:
    """A non-preferred candidate and a metric carrying both it and the preferred one.

    The metric is None when no candidate pairs with the preferred one on any
    metric; the first non-preferred candidate is returned then.
    """
    preferred = trap.preferred
    if preferred is not None:
        for c in trap.candidates:
            if c == preferred:
                continue
            metric = _metric_with(catalog, [preferred, c])
            if metric is not None:
                return c, metric
    return _non_preferred(trap), None


def _non_preferred(trap: _ChoiceTrap) -> str:
    """A candidate other than the preferred one, so a prefer has something to swap."""
    for c in trap.candidates:
        if c != trap.preferred:
            return c
    return trap.candidates[0]


def _first_metric(catalog: Catalog) -> str | None:
    return next(iter(catalog.metrics), None)


def _metric_with(catalog: Catalog, dimensions: list[str]) -> str | None:
    """The first metric that carries every one of `dimensions`."""
    for name, metric in catalog.metrics.items():
        if all(d in metric.dimensions for d in dimensions):
            return name
    return None


def _trap_label(name: str, catalog: Catalog) -> str:
    """How a trap disclosure names a candidate: its label, else its raw name."""
    info = catalog.metrics.get(name) or catalog.dimensions.get(name)
    return (info.label if info is not None and info.label else None) or name


def _first_categorical(metric: MetricInfo, catalog: Catalog) -> str | None:
    for name in metric.dimensions:
        dim = catalog.dimensions.get(name)
        if dim is not None and dim.type == "categorical":
            return name
    return None


def _short(dimension: str) -> str:
    return dimension.split("__", 1)[-1]


def _dim_words(dimension: str) -> str:
    return dimension.replace("__", " ").replace("_", " ")


def _first_sentence(text: str) -> str:
    return text.split(".")[0].strip()


# --------------------------------------------------------------------------- #
# Writing and approving
# --------------------------------------------------------------------------- #


def append_items(path: str | Path, items: list[GoldenItem]) -> list[GoldenItem]:
    """Add items whose ids are new to an existing set, in place. Returns those added.

    The file is hand-edited, so the new blocks go on the end and nothing above
    them moves.
    """
    from understory.harness.golden import load_golden

    p = Path(path)
    existing = {i.id for i in load_golden(p)}
    fresh = [i for i in items if i.id not in existing]
    if not fresh:
        return []
    blocks = dump_items(fresh).split("questions:\n", 1)[1]
    text = p.read_text().rstrip("\n") + "\n\n" + blocks
    p.write_text(text)
    return fresh


_VERIFIED_LINE = "    verified: true"
_BLOCK_KEYS = ("    notes:", "    spec:", "    expected:")


def set_verified(path: str | Path, ids: list[str], value: bool = True) -> list[str]:
    """Set or clear `verified` on the named items, editing lines in place.

    Returns the ids found. The flag sits after `source` (or `kind`, or the
    question) and before `notes`, the place `dump_items` puts it.
    """
    p = Path(path)
    lines = p.read_text().splitlines()
    wanted = set(ids)
    found: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        if not line.startswith("  - id: "):
            continue
        item_id = line[len("  - id: ") :].strip()
        block: list[str] = []
        while i < len(lines) and not lines[i].startswith("  - id: "):
            block.append(lines[i])
            i += 1
        if item_id in wanted:
            found.append(item_id)
            block = [ln for ln in block if not ln.startswith("    verified:")]
            if value:
                at = next(
                    (k for k, ln in enumerate(block) if ln.startswith(_BLOCK_KEYS)), len(block)
                )
                block.insert(at, _VERIFIED_LINE)
        out.extend(block)
    p.write_text("\n".join(out).rstrip("\n") + "\n")
    return found


__all__ = ["append_items", "draft_golden", "last_full_month", "set_verified"]
