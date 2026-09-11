"""Pure translation from a MetricSpec's filter and time parts to MetricFlow inputs.

Both semantic layer backends speak the same filter language: a SQL-like string
where every dimension reference is wrapped in MetricFlow's Jinja template call,
`{{ Dimension('order__country') }} IN ('US', 'CA')`. Keeping this translation
in one place means the local CLI and the dbt Cloud SDK produce the same SQL for
the same spec.

Nothing here touches the filesystem or a subprocess, so it is cheap to unit test.
"""

from __future__ import annotations

from understory.types import MetricSpec, WhereClause, WhereOp

_SCALAR_OPS: dict[WhereOp, str] = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}

_LIST_OPS: dict[WhereOp, str] = {
    "in": "IN",
    "not_in": "NOT IN",
}

TIME_GRAINS = ("day", "week", "month", "quarter", "year")


def sql_literal(value: str | int | float) -> str:
    """Render one value as a SQL literal.

    Strings are single-quoted with embedded quotes doubled. Numbers are left
    bare. Booleans are a subclass of int in Python; the spec type does not admit
    them, so they are not special-cased.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def clause_to_mf(clause: WhereClause) -> str:
    """Render one WhereClause as a MetricFlow filter expression."""
    ref = f"{{{{ Dimension('{clause.dimension}') }}}}"
    if clause.op in _LIST_OPS:
        if not clause.values:
            # An empty IN list is invalid SQL. Make the intent explicit instead.
            return "1 = 0" if clause.op == "in" else "1 = 1"
        rendered = ", ".join(sql_literal(v) for v in clause.values)
        return f"{ref} {_LIST_OPS[clause.op]} ({rendered})"
    if clause.op in _SCALAR_OPS:
        if len(clause.values) != 1:
            raise ValueError(
                f"where clause on {clause.dimension!r} with op {clause.op!r} "
                f"needs exactly one value, got {len(clause.values)}"
            )
        return f"{ref} {_SCALAR_OPS[clause.op]} {sql_literal(clause.values[0])}"
    raise ValueError(f"unsupported where op {clause.op!r}")


def to_mf_where(clauses: list[WhereClause]) -> str | None:
    """Join every clause with AND into one MetricFlow `--where` string.

    Returns None when there are no clauses so callers can omit the flag.
    """
    if not clauses:
        return None
    return " AND ".join(clause_to_mf(c) for c in clauses)


def _strip_grain(name: str) -> tuple[str, str | None]:
    """Split `metric_time__month` into (`metric_time`, `month`)."""
    for grain in TIME_GRAINS:
        suffix = f"__{grain}"
        if name.endswith(suffix):
            return name[: -len(suffix)], grain
    return name, None


def time_group_by(spec: MetricSpec, catalog_time_dim: str | None) -> list[str]:
    """Return the spec's group_by with the time grain applied.

    MetricFlow expresses grain as a suffix on the time dimension, so a spec with
    `grain=month` and `group_by=[metric_time]` becomes `metric_time__month`.
    The rules, in order:

    * With no grain, group_by is returned unchanged.
    * A bare `metric_time`, or the metric's own agg time dimension as named in
      the catalog, gets the grain appended.
    * An entry that already carries a grain suffix is left alone. The caller
      asked for that grain explicitly.
    * If no time dimension is present at all, `metric_time__<grain>` is
      appended so the grain is not silently dropped.
    """
    grain = spec.time.grain
    if grain is None:
        return list(spec.group_by)

    time_names = {"metric_time"}
    if catalog_time_dim:
        time_names.add(catalog_time_dim)

    out: list[str] = []
    saw_time = False
    for name in spec.group_by:
        base, existing = _strip_grain(name)
        if base in time_names:
            saw_time = True
            out.append(name if existing else f"{base}__{grain}")
        else:
            out.append(name)
    if not saw_time:
        out.append(f"metric_time__{grain}")
    return out
