"""CI check for a traps registry against a catalog.

`check_registry` returns human-readable problems. An empty list means the
registry is clean. It runs in CI through `understory traps check <tenant_dir>`
and in tests against every committed tenant.
"""

from __future__ import annotations

from understory.traps.schema import KNOWN_CONVENTIONS, WINDOWS, Registry, normalize
from understory.types import Catalog


def check_registry(registry: Registry, catalog: Catalog) -> list[str]:
    problems: list[str] = []

    for trap in registry.collisions:
        for c in trap.candidates:
            if c not in catalog.metrics:
                problems.append(f"{trap.id}: candidate metric {c!r} is not in the catalog")
        if trap.preferred is not None and trap.preferred not in trap.candidates:
            problems.append(
                f"{trap.id}: prefer target {trap.preferred!r} is not among the candidates"
            )
        for h in trap.hint:
            if h not in trap.candidates:
                problems.append(f"{trap.id}: hint for {h!r} which is not a candidate")

    for trap in registry.dimension_roles:
        for c in trap.candidates:
            if c not in catalog.dimensions:
                problems.append(f"{trap.id}: candidate dimension {c!r} is not in the catalog")
        if trap.preferred is not None and trap.preferred not in trap.candidates:
            problems.append(
                f"{trap.id}: prefer target {trap.preferred!r} is not among the candidates"
            )

    seen_conventions: set[str] = set()
    for conv in registry.conventions:
        if conv.name in seen_conventions:
            problems.append(f"{conv.id}: declared more than once")
        seen_conventions.add(conv.name)
        if conv.name not in KNOWN_CONVENTIONS:
            problems.append(
                f"{conv.id}: unknown convention; known ones are {', '.join(KNOWN_CONVENTIONS)}"
            )
        if conv.name == "time_anchor" and conv.policy == "ask_if_absent":
            problems.append(f"{conv.id}: time_anchor cannot be asked, only disclosed")
        if conv.name == "default_window" and conv.value not in WINDOWS:
            problems.append(
                f"{conv.id}: value {conv.value!r} is not a known window ({', '.join(WINDOWS)})"
            )

    seen_phrases: dict[str, str] = {}
    for trap in [*registry.collisions, *registry.dimension_roles, *registry.unanswerable]:
        for p in trap.phrase:
            key = normalize(p)
            if not key:
                problems.append(f"{trap.id}: empty phrase")
            elif key in seen_phrases and seen_phrases[key] != trap.id:
                problems.append(f"phrase {p!r} appears in both {seen_phrases[key]} and {trap.id}")
            else:
                seen_phrases[key] = trap.id

    problems.extend(_unadjudicated_synonyms(registry, catalog))
    return problems


def _unadjudicated_synonyms(registry: Registry, catalog: Catalog) -> list[str]:
    """Synonyms declared on more than one metric must be covered by a collision entry."""
    by_synonym: dict[str, set[str]] = {}
    for metric in catalog.metrics.values():
        for s in metric.synonyms:
            by_synonym.setdefault(normalize(s), set()).add(metric.name)

    problems: list[str] = []
    for synonym, metrics in sorted(by_synonym.items()):
        if len(metrics) < 2:
            continue
        covered = any(
            synonym in {normalize(p) for p in trap.phrase} and metrics <= set(trap.candidates)
            for trap in registry.collisions
        )
        if not covered:
            problems.append(
                f"synonym {synonym!r} maps to {sorted(metrics)} and no collision entry "
                "with that phrase covers all of them"
            )
    return problems
