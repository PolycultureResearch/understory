"""Assemble the get_context document.

The document is the tenant's hand-written `context.md`, verbatim and first,
followed by a generated tail: the metrics grouped by semantic model, the
dimensions, the query conventions with data freshness, and the schemas
`run_sql` may touch. The tail is kept short because the whole document
should stay within a few kilobytes; the Cube benchmark that motivates this
tool found that a 4 KB document moved accuracy more than model choice did.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date

from understory.catalog.manifest import METRIC_TIME
from understory.tenant import TenantConfig
from understory.types import Catalog, MetricInfo

_DESCRIPTION_MAX = 80


def build_context(
    tenant: TenantConfig,
    catalog: Catalog,
    data_through: dict[str, date] | None = None,
) -> str:
    """The full get_context markdown for a tenant.

    `data_through` maps time dimension names (`order__order_date`) to the
    latest date the warehouse holds for them. When given, freshness is stated
    per time dimension in the conventions section.
    """
    parts: list[str] = []
    if tenant.context_path.exists():
        parts.append(tenant.context_path.read_text().strip())
    else:
        parts.append(f"# {tenant.display_name}")
    parts.append(metrics_section(catalog))
    parts.append(dimensions_section(catalog))
    parts.append(conventions_section(catalog, data_through))
    parts.append(sql_section(tenant, catalog))
    return "\n\n".join(parts) + "\n"


def metrics_section(catalog: Catalog) -> str:
    groups: OrderedDict[str, list[MetricInfo]] = OrderedDict()
    for m in catalog.metrics.values():
        groups.setdefault(_primary_model(m), []).append(m)
    lines = ["## Metrics", "", "Grouped by the table each one is measured on."]
    for model, metrics in groups.items():
        lines.append("")
        lines.append(f"{model}:")
        for m in metrics:
            head = m.type if _label_is_redundant(m) else f"{m.label}, {m.type}"
            lines.append(f"- {m.name} ({head}): {_one_line(m.description)}")
    return "\n".join(lines)


def dimensions_section(catalog: Catalog) -> str:
    by_entity: OrderedDict[str, list[str]] = OrderedDict()
    for dim in catalog.dimensions.values():
        if dim.name == METRIC_TIME:
            continue
        entity, _, short = dim.name.partition("__")
        text = f"{short} (time)" if dim.type == "time" else short
        by_entity.setdefault(entity, []).append(text)
    lines = [
        "## Dimensions",
        "",
        "Names are entity__dimension. describe_metric lists which apply to a metric.",
        f"- {METRIC_TIME} (time): every metric's own time axis; use it for time series.",
    ]
    for entity, dims in by_entity.items():
        lines.append(f"- {entity}__: {', '.join(dims)}")
    return "\n".join(lines)


def conventions_section(catalog: Catalog, data_through: dict[str, date] | None) -> str:
    lines = [
        "## Conventions",
        "",
        "- Time windows anchor to the latest date the data holds, never to today. An omitted "
        "end date resolves to that date and the applied window is always disclosed.",
        f"- Group time series by {METRIC_TIME} at day, week, month, quarter, or year grain.",
        "- Ratio and derived metrics are computed from their inputs over the whole window, "
        "not averaged from daily values.",
        "- Each metric is dated by its own time dimension (listed by describe_metric). "
        "Metrics on different tables can have different freshness.",
    ]
    if data_through:
        lines.append("- Data through, per time dimension:")
        for dim, d in data_through.items():
            lines.append(f"  - {dim}: {d.isoformat()}")
    else:
        lines.append("- Freshness is computed per request and reported in provenance.")
    return "\n".join(lines)


def sql_section(tenant: TenantConfig, catalog: Catalog) -> str:
    relations = _relations(catalog)
    schemas = [s.lower() for s in tenant.sql.schemas]
    lines = ["## SQL scope", ""]
    if not tenant.sql.enabled:
        lines.append("run_sql is disabled for this tenant.")
        return "\n".join(lines)
    if schemas:
        lines.append(
            f"run_sql may read schemas {', '.join(schemas)} and information_schema. "
            "Results are ungoverned and must be labeled as such."
        )
    else:
        lines.append(
            "run_sql may read every schema the semantic layer references, and "
            "information_schema. Results are ungoverned and must be labeled as such."
        )
    lines.append("Tables the semantic layer is built on:")
    for rel in relations:
        note = "" if not schemas or _schema_of(rel) in schemas else " (metrics only, outside scope)"
        lines.append(f"- {_short_relation(rel)}{note}")
    if catalog.time_spine:
        lines.append(f"- {_short_relation(catalog.time_spine)} (time spine)")
    return "\n".join(lines)


def _primary_model(m: MetricInfo) -> str:
    models = (m.meta.get("understory") or {}).get("semantic_models") or []
    return models[0] if models else "other"


def _label_is_redundant(m: MetricInfo) -> bool:
    """True when the label is just the name with spaces, so listing it adds nothing."""
    if not m.label:
        return True
    return re.sub(r"[\s-]+", "_", m.label.strip().lower()) == m.name.lower()


def _one_line(text: str | None) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat, maxsplit=1)[0]
    if len(first) > _DESCRIPTION_MAX:
        first = first[: _DESCRIPTION_MAX - 3].rstrip() + "..."
    return first


def _relations(catalog: Catalog) -> list[str]:
    seen: list[str] = []
    for dim in catalog.dimensions.values():
        if dim.relation and dim.relation != catalog.time_spine and dim.relation not in seen:
            seen.append(dim.relation)
    for m in catalog.metrics.values():
        for rel in m.relations:
            if rel not in seen:
                seen.append(rel)
    return seen


def _short_relation(relation: str) -> str:
    """schema.table for a quoted three-part relation name."""
    parts = [p.strip('"') for p in relation.split(".")]
    return ".".join(parts[-2:]) if len(parts) >= 2 else relation


def _schema_of(relation: str) -> str:
    parts = [p.strip('"') for p in relation.split(".")]
    return parts[-2].lower() if len(parts) >= 2 else ""
