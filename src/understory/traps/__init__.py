"""Traps registry: schema, matcher, and the CI compile check.

The registry (traps.yml) holds everything about how to resolve a question that
is not a metric definition. `check` runs it inside query_metrics;
`check_registry` runs in CI.
"""

from understory.traps.compile import check_registry
from understory.traps.match import check
from understory.traps.schema import (
    WINDOWS,
    CollisionTrap,
    ConventionTrap,
    DimensionRoleTrap,
    Registry,
    UnanswerableTrap,
    load_registry,
    normalize,
)

__all__ = [
    "WINDOWS",
    "CollisionTrap",
    "ConventionTrap",
    "DimensionRoleTrap",
    "Registry",
    "UnanswerableTrap",
    "check",
    "check_registry",
    "load_registry",
    "normalize",
]
