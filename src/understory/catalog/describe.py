"""Discovery views over the catalog: list_metrics, describe_metric, search_dimensions.

These return plain dicts and lists so the server can hand them to the MCP
layer without another conversion. Nothing here touches a warehouse.
"""

from __future__ import annotations

from typing import Any

from understory.types import Catalog, DimensionInfo, MetricInfo, MetricSpec

_EXAMPLE_VALUE = "<value from search_dimension_values>"


def list_metrics(catalog: Catalog) -> list[dict[str, Any]]:
    """Compact rows for the list_metrics tool, in catalog order."""
    return [
        {
            "name": m.name,
            "label": m.label,
            "description": m.description,
            "type": m.type,
            "dimensions": len(m.dimensions),
            "synonyms": list(m.synonyms),
        }
        for m in catalog.metrics.values()
    ]


def describe_metric(catalog: Catalog, name: str) -> dict[str, Any]:
    """Everything the catalog knows about one metric, plus example specs.

    An unknown name returns `{"error": ..., "nearest": [...]}` rather than
    raising, so the server can pass it through as an `invalid` response.
    """
    metric = catalog.metric(name)
    if metric is None:
        return {
            "error": f"unknown metric {name!r}",
            "nearest": catalog.nearest_metrics(name),
        }
    out = metric.model_dump(mode="json")
    out["dimension_details"] = [
        _dimension_row(catalog.dimensions[d]) for d in metric.dimensions if d in catalog.dimensions
    ]
    out["examples"] = example_specs(catalog, metric)
    return out


def example_specs(catalog: Catalog, metric: MetricInfo) -> list[dict[str, Any]]:
    """A few valid query_metrics specs for the metric, as JSON-able dicts."""
    label = metric.label or metric.name
    categorical = [
        d
        for d in metric.dimensions
        if d in catalog.dimensions and catalog.dimensions[d].type == "categorical"
    ]
    specs: list[MetricSpec] = [
        MetricSpec(
            metrics=[metric.name],
            time={"grain": "month"},
            question=f"{label} by month",
        )
    ]
    if categorical:
        dim = categorical[0]
        specs.append(
            MetricSpec(
                metrics=[metric.name],
                group_by=[dim],
                time={"grain": "month"},
                question=f"{label} by {_short(dim)} and month",
            )
        )
        specs.append(
            MetricSpec(
                metrics=[metric.name],
                where=[{"dimension": dim, "op": "in", "values": [_EXAMPLE_VALUE]}],
                time={"grain": "week"},
                question=f"{label} by week for one {_short(dim)}",
            )
        )
    if metric.inputs:
        specs.append(
            MetricSpec(
                metrics=[metric.name, *metric.inputs],
                time={"grain": "month"},
                question=f"{label} with its inputs by month",
            )
        )
    return [s.model_dump(mode="json", exclude_defaults=True) for s in specs]


def search_dimensions(catalog: Catalog, q: str) -> list[DimensionInfo]:
    """Dimensions whose name, label, or description contains `q` (case-insensitive)."""
    needle = q.strip().lower()
    if not needle:
        return []
    hits: list[DimensionInfo] = []
    for dim in catalog.dimensions.values():
        haystack = " ".join(filter(None, [dim.name, dim.label, dim.description])).lower()
        if needle in haystack:
            hits.append(dim)
    return hits


def _dimension_row(dim: DimensionInfo) -> dict[str, Any]:
    row: dict[str, Any] = {"name": dim.name, "type": dim.type}
    if dim.label:
        row["label"] = dim.label
    if dim.description:
        row["description"] = dim.description
    if dim.time_granularity:
        row["time_granularity"] = dim.time_granularity
    return row


def _short(dimension: str) -> str:
    return dimension.split("__", 1)[-1].replace("_", " ")
