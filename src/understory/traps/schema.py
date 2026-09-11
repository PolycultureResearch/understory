"""Pydantic models for traps.yml and the loader.

The registry holds everything about how to resolve a question that is not a
metric definition: which phrases collide, which dimension roles collide, which
conventions to ask about or disclose, and which concepts are declared out of
scope. See design section 5 for the YAML shape.

Every entry has a stable id. It is either declared with `id:` or derived from
the entry kind and its first phrase, e.g. `collision:revenue`,
`dimension_role:region`, `convention:default_window`,
`unanswerable:profit_by_sku`. Clarification choices reference these ids.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PREFER = re.compile(r"^prefer\s+(\S+)$")
_NON_WORD = re.compile(r"[^0-9a-z]+")

WINDOWS: dict[str, str] = {
    "trailing_7_days": "Trailing 7 days",
    "trailing_30_days": "Trailing 30 days",
    "trailing_90_days": "Trailing 90 days",
    "last_month": "Last full month",
    "last_quarter": "Last full quarter",
    "year_to_date": "Year to date",
}
"""Window ids the server knows how to turn into dates. Keys are option ids."""

KNOWN_CONVENTIONS = ("time_anchor", "default_window")


def normalize(text: str) -> str:
    """Lowercase, replace punctuation and underscores with spaces, collapse whitespace."""
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def slug(phrase: str) -> str:
    return normalize(phrase).replace(" ", "_")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _listify(value: object) -> object:
    return [value] if isinstance(value, str) else value


class _ChoiceTrap(_Strict):
    """Shared shape of collisions and dimension roles: phrases, candidates, a policy."""

    kind: ClassVar[str] = ""

    id: str = ""
    phrase: list[str] = Field(min_length=1)
    candidates: list[str] = Field(min_length=1)
    policy: str = "ask"
    """`ask` or `prefer <candidate>`."""
    priority: int = 100
    """Lower sorts first when several clarifications fire."""

    _phrase_list = field_validator("phrase", mode="before")(_listify)

    @field_validator("policy")
    @classmethod
    def _valid_policy(cls, value: str) -> str:
        value = " ".join(value.split())
        if value == "ask" or _PREFER.match(value):
            return value
        raise ValueError(f"policy must be 'ask' or 'prefer <candidate>', got {value!r}")

    @model_validator(mode="after")
    def _finish(self) -> _ChoiceTrap:
        # A prefer target outside the candidates is reported by check_registry
        # rather than failing the load, so a tenant with a typo still boots.
        if not self.id:
            self.id = f"{self.kind}:{slug(self.phrase[0])}"
        return self

    @property
    def is_ask(self) -> bool:
        return self.policy == "ask"

    @property
    def preferred(self) -> str | None:
        m = _PREFER.match(self.policy)
        return m.group(1) if m else None


class CollisionTrap(_ChoiceTrap):
    """A phrase that maps to more than one metric."""

    kind: ClassVar[str] = "collision"

    hint: dict[str, str] = Field(default_factory=dict)
    """Candidate metric name to a one-line hint shown with the option."""


class DimensionRoleTrap(_ChoiceTrap):
    """A phrase that maps to more than one dimension, e.g. customer vs order country."""

    kind: ClassVar[str] = "dimension_role"

    disclose: str | None = None
    """Text disclosed when the preferred or chosen dimension is applied."""


class ConventionTrap(_Strict):
    """A resolution convention with no entity to hang off, such as the time anchor."""

    name: str
    value: str
    disclose: bool | str = True
    """False for silent, True for generated text, or the text itself."""
    policy: Literal["ask_if_absent", "disclose"] = "disclose"
    priority: int = 100

    @property
    def id(self) -> str:
        return f"convention:{self.name}"

    @property
    def disclose_text(self) -> str | None:
        """Declared text, or None when the text should be generated or suppressed."""
        return self.disclose if isinstance(self.disclose, str) else None

    @property
    def wants_disclosure(self) -> bool:
        return self.disclose is not False


class UnanswerableTrap(_Strict):
    """A phrase the semantic layer cannot answer, with the reason to give."""

    id: str = ""
    phrase: list[str] = Field(min_length=1)
    reason: str

    _phrase_list = field_validator("phrase", mode="before")(_listify)

    @model_validator(mode="after")
    def _finish(self) -> UnanswerableTrap:
        if not self.id:
            self.id = f"unanswerable:{slug(self.phrase[0])}"
        return self


class Registry(_Strict):
    version: int = 1
    collisions: list[CollisionTrap] = Field(default_factory=list)
    dimension_roles: list[DimensionRoleTrap] = Field(default_factory=list)
    conventions: list[ConventionTrap] = Field(default_factory=list)
    unanswerable: list[UnanswerableTrap] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> Registry:
        seen: set[str] = set()
        for trap_id in self.trap_ids():
            if trap_id in seen:
                raise ValueError(f"duplicate trap id {trap_id!r}; set an explicit id")
            seen.add(trap_id)
        return self

    def trap_ids(self) -> list[str]:
        return (
            [t.id for t in self.collisions]
            + [t.id for t in self.dimension_roles]
            + [t.id for t in self.conventions]
            + [t.id for t in self.unanswerable]
        )

    def is_empty(self) -> bool:
        return not (
            self.collisions or self.dimension_roles or self.conventions or self.unanswerable
        )


def load_registry(path: str | Path) -> Registry:
    """Load traps.yml. A missing or empty file yields an empty registry."""
    p = Path(path)
    if not p.exists():
        return Registry()
    raw = yaml.safe_load(p.read_text())
    if raw is None:
        return Registry()
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a mapping at the top level")
    return Registry.model_validate(raw)
