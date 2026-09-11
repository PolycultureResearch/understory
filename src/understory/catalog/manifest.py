"""Read a dbt `semantic_manifest.json` into the `Catalog` type.

The manifest is what `dbt parse` writes for MetricFlow. Two metric specs are
in circulation and both are handled here:

- the classic spec, where a simple metric names a `measure` defined on a
  semantic model and the measure carries the aggregation;
- the newer spec, where the metric carries the aggregation inline under
  `type_params.metric_aggregation_params` and `type_params.expr`.

Dimension names follow MetricFlow's `entity__dimension` convention: the
primary entity of the semantic model, two underscores, the dimension name.
A metric can reach the dimensions of its own semantic models plus the
dimensions of any semantic model joinable through a shared entity, one hop,
many-to-one only (the target model must hold the entity as primary or
unique). `metric_time` is added to every metric as its own time axis.

When the aggregation of a metric cannot be determined the loader raises
`CatalogError` naming the metric. Guessing would put a wrong number in front
of a user, which is the one thing Understory must not do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from understory.types import Catalog, DimensionInfo, MetricInfo

METRIC_TIME = "metric_time"

_JOIN_ENTITY_TYPES = {"primary", "unique"}


class CatalogError(ValueError):
    """The manifest cannot be turned into a catalog. The message names the offender."""


@dataclass
class _Model:
    """The parts of a semantic model the catalog needs."""

    name: str
    relation: str
    primary_entity: str
    entities: dict[str, str]
    """Entity name to entity type (primary, unique, foreign, natural)."""
    dimensions: list[dict[str, Any]]
    measures: dict[str, dict[str, Any]]
    default_time_dimension: str | None
    description: str | None = None
    local_dims: list[str] = field(default_factory=list)
    """Qualified names of this model's own dimensions, in manifest order."""

    def qualified(self, dim: str, entity: str | None = None) -> str:
        return f"{entity or self.primary_entity}__{dim}"

    def join_entities(self) -> set[str]:
        """Entities another model may join to this one through, many-to-one."""
        return {e for e, t in self.entities.items() if t in _JOIN_ENTITY_TYPES}


@dataclass
class _Resolved:
    """A metric's measures resolved to the semantic models they live on."""

    pairs: list[tuple[_Model, dict[str, Any]]] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def load_catalog(path: str | Path) -> Catalog:
    """Parse a semantic_manifest.json file into a Catalog."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise CatalogError(f"semantic manifest not found at {p}") from e
    except json.JSONDecodeError as e:
        raise CatalogError(f"semantic manifest at {p} is not valid JSON: {e}") from e
    return catalog_from_manifest(raw)


def catalog_from_manifest(raw: dict[str, Any]) -> Catalog:
    """Build a Catalog from an already-parsed manifest dict."""
    models = [_parse_model(sm) for sm in raw.get("semantic_models", [])]
    by_name = {m.name: m for m in models}
    time_spine, spine_column, spine_grain = _time_spine(raw.get("project_configuration") or {})

    dimensions = _collect_dimensions(models, time_spine, spine_column, spine_grain)
    reachable = {m.name: _reachable_dimensions(m, models) for m in models}

    raw_metrics = {m["name"]: m for m in raw.get("metrics", [])}
    resolved: dict[str, _Resolved] = {}
    for name in raw_metrics:
        _resolve(name, raw_metrics, by_name, resolved, stack=())

    metrics: dict[str, MetricInfo] = {}
    for name, rm in raw_metrics.items():
        metrics[name] = _metric_info(rm, resolved, raw_metrics, reachable)

    return Catalog(metrics=metrics, dimensions=dimensions, time_spine=time_spine)


# --------------------------------------------------------------------------- #
# Semantic models and dimensions
# --------------------------------------------------------------------------- #


def _parse_model(sm: dict[str, Any]) -> _Model:
    name = sm.get("name") or "<unnamed>"
    node = sm.get("node_relation") or {}
    relation = node.get("relation_name") or _compose_relation(node)
    entities = {e["name"]: (e.get("type") or "").lower() for e in sm.get("entities", [])}
    primary = next((e for e, t in entities.items() if t == "primary"), None)
    if primary is None:
        primary = sm.get("primary_entity")
    if not primary:
        raise CatalogError(
            f"semantic model {name!r} has no primary entity, so its dimensions "
            "cannot be named with the entity__dimension convention"
        )
    if primary not in entities:
        entities[primary] = "primary"
    defaults = sm.get("defaults") or {}
    model = _Model(
        name=name,
        relation=relation,
        primary_entity=primary,
        entities=entities,
        dimensions=list(sm.get("dimensions") or []),
        measures={m["name"]: m for m in sm.get("measures") or []},
        default_time_dimension=defaults.get("agg_time_dimension"),
        description=sm.get("description"),
    )
    model.local_dims = [model.qualified(d["name"]) for d in model.dimensions]
    return model


def _compose_relation(node: dict[str, Any]) -> str:
    parts = [node.get("database"), node.get("schema_name"), node.get("alias")]
    return ".".join(f'"{p}"' for p in parts if p)


def _dimension_info(model: _Model, dim: dict[str, Any], entity: str) -> DimensionInfo:
    dtype = (dim.get("type") or "categorical").lower()
    if dtype not in ("categorical", "time"):
        raise CatalogError(
            f"dimension {dim.get('name')!r} on semantic model {model.name!r} "
            f"has unsupported type {dtype!r}"
        )
    params = dim.get("type_params") or {}
    return DimensionInfo(
        name=model.qualified(dim["name"], entity),
        label=dim.get("label"),
        description=dim.get("description"),
        type=dtype,
        time_granularity=params.get("time_granularity") if dtype == "time" else None,
        semantic_model=model.name,
        relation=model.relation,
        column=dim.get("expr") or dim["name"],
    )


def _collect_dimensions(
    models: list[_Model],
    time_spine: str | None,
    spine_column: str | None,
    spine_grain: str | None,
) -> dict[str, DimensionInfo]:
    """Every dimension the catalog knows, keyed by qualified name.

    Local dimensions are registered under the model's primary entity. A model
    that also carries a unique entity gets its dimensions registered under
    that entity too, since a join through it produces that name. The first
    definition of a name wins; MetricFlow requires duplicates to agree.
    """
    out: dict[str, DimensionInfo] = {}
    out[METRIC_TIME] = DimensionInfo(
        name=METRIC_TIME,
        label="Metric time",
        description=(
            "Every metric's own time axis, its aggregation time dimension. "
            "Use it to group or filter any metric by time."
        ),
        type="time",
        time_granularity=spine_grain or "day",
        semantic_model="time_spine",
        relation=time_spine or "",
        column=spine_column or "date_day",
    )
    for model in models:
        for entity in [model.primary_entity, *sorted(model.join_entities())]:
            for dim in model.dimensions:
                info = _dimension_info(model, dim, entity)
                out.setdefault(info.name, info)
    return out


def _reachable_dimensions(model: _Model, models: list[_Model]) -> list[str]:
    """Dimensions a measure on `model` may be grouped by: local plus one hop."""
    names = list(model.local_dims)
    seen = set(names)
    for entity in model.entities:
        for other in models:
            if other is model or entity not in other.join_entities():
                continue
            for dim in other.dimensions:
                qualified = other.qualified(dim["name"], entity)
                if qualified not in seen:
                    seen.add(qualified)
                    names.append(qualified)
    return names


def _time_spine(project: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    spines = project.get("time_spines") or []
    if spines:
        spine = spines[0]
        node = spine.get("node_relation") or {}
        relation = node.get("relation_name") or _compose_relation(node)
        column = spine.get("primary_column") or {}
        return relation, column.get("name"), column.get("time_granularity")
    legacy = project.get("time_spine_table_configurations") or []
    if legacy:
        cfg = legacy[0]
        return cfg.get("location"), cfg.get("column_name"), cfg.get("grain")
    return None, None, None


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def _resolve(
    name: str,
    raw_metrics: dict[str, dict[str, Any]],
    models: dict[str, _Model],
    out: dict[str, _Resolved],
    stack: tuple[str, ...],
) -> _Resolved:
    """Resolve a metric to (model, measure) pairs, following inputs recursively."""
    if name in out:
        return out[name]
    if name in stack:
        raise CatalogError(f"metric {name!r} depends on itself through {' -> '.join(stack)}")
    rm = raw_metrics.get(name)
    if rm is None:
        raise CatalogError(f"metric {stack[-1]!r} references unknown metric {name!r}")

    mtype = (rm.get("type") or "").lower()
    tp = rm.get("type_params") or {}
    res = _Resolved(filters=_filter_strings(rm.get("filter")))

    if mtype in ("simple", "cumulative"):
        model, measure = _measure_for(rm, models)
        res.pairs.append((model, measure))
        res.filters += _filter_strings(_measure_ref(tp).get("filter"), measure.get("filter"))
    elif mtype == "conversion":
        ctp = tp.get("conversion_type_params") or {}
        for key in ("base_measure", "conversion_measure"):
            ref = ctp.get(key)
            if not ref:
                raise CatalogError(f"conversion metric {name!r} has no {key}")
            model, measure = _find_measure(_ref_name(ref), models, name)
            res.pairs.append((model, measure))
            res.filters += _filter_strings(
                ref.get("filter") if isinstance(ref, dict) else None, measure.get("filter")
            )
    elif mtype == "ratio":
        for key in ("numerator", "denominator"):
            ref = tp.get(key)
            if not ref:
                raise CatalogError(f"ratio metric {name!r} has no {key}")
            _add_input(res, ref, name, raw_metrics, models, out, stack)
    elif mtype == "derived":
        refs = tp.get("metrics") or []
        if not refs:
            raise CatalogError(f"derived metric {name!r} lists no input metrics")
        for ref in refs:
            _add_input(res, ref, name, raw_metrics, models, out, stack)
    else:
        raise CatalogError(f"metric {name!r} has unsupported type {mtype!r}")

    out[name] = res
    return res


def _add_input(
    res: _Resolved,
    ref: Any,
    parent: str,
    raw_metrics: dict[str, dict[str, Any]],
    models: dict[str, _Model],
    out: dict[str, _Resolved],
    stack: tuple[str, ...],
) -> None:
    input_name = _ref_name(ref)
    res.inputs.append(input_name)
    res.filters += _filter_strings(ref.get("filter") if isinstance(ref, dict) else None)
    child = _resolve(input_name, raw_metrics, models, out, (*stack, parent))
    for pair in child.pairs:
        if pair not in res.pairs:
            res.pairs.append(pair)


def _measure_ref(tp: dict[str, Any]) -> dict[str, Any]:
    ref = tp.get("measure")
    if isinstance(ref, str):
        return {"name": ref}
    return ref or {}


def _ref_name(ref: Any) -> str:
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict) and ref.get("name"):
        return ref["name"]
    raise CatalogError(f"metric input reference {ref!r} has no name")


def _find_measure(
    measure_name: str, models: dict[str, _Model], metric: str
) -> tuple[_Model, dict[str, Any]]:
    for model in models.values():
        if measure_name in model.measures:
            return model, model.measures[measure_name]
    raise CatalogError(
        f"metric {metric!r} uses measure {measure_name!r}, which no semantic model defines"
    )


def _measure_for(rm: dict[str, Any], models: dict[str, _Model]) -> tuple[_Model, dict[str, Any]]:
    """The measure a simple or cumulative metric aggregates, from either spec."""
    name = rm["name"]
    tp = rm.get("type_params") or {}
    ref = _measure_ref(tp)
    if ref.get("name"):
        return _find_measure(ref["name"], models, name)

    params = tp.get("metric_aggregation_params")
    if params:
        model_name = params.get("semantic_model")
        model = models.get(model_name or "")
        if model is None:
            raise CatalogError(
                f"metric {name!r} names semantic model {model_name!r} in its "
                "metric_aggregation_params, which the manifest does not define"
            )
        if not params.get("agg"):
            raise CatalogError(f"metric {name!r} has metric_aggregation_params without an agg")
        measure = {
            "name": name,
            "agg": params["agg"],
            "expr": tp.get("expr") or name,
            "agg_params": params.get("agg_params"),
            "agg_time_dimension": params.get("agg_time_dimension"),
            "non_additive_dimension": params.get("non_additive_dimension"),
            "filter": None,
        }
        return model, measure

    raise CatalogError(
        f"cannot determine the aggregation for metric {name!r}: it names no measure "
        "and carries no metric_aggregation_params"
    )


def _filter_strings(*filters: Any) -> list[str]:
    """Flatten the shapes a filter takes in the manifest into SQL template strings."""
    out: list[str] = []
    for f in filters:
        if f is None:
            continue
        if isinstance(f, str):
            out.append(f.strip())
        elif isinstance(f, list):
            out += _filter_strings(*f)
        elif isinstance(f, dict):
            if "where_sql_template" in f:
                out.append(str(f["where_sql_template"]).strip())
            elif "where_filters" in f:
                out += _filter_strings(*(f["where_filters"] or []))
    return [s for s in out if s]


def _agg_expr(measure: dict[str, Any]) -> str:
    agg = (measure.get("agg") or "").lower()
    expr = measure.get("expr") or measure.get("name") or ""
    if agg == "percentile":
        params = measure.get("agg_params") or {}
        p = params.get("percentile")
        kind = "discrete" if params.get("use_discrete_percentile") else "continuous"
        return f"percentile({expr}, {p}, {kind})"
    if agg == "sum_boolean":
        return f"sum(case when {expr} then 1 else 0 end)"
    return f"{agg}({expr})"


def _window_text(window: Any) -> str | None:
    if not window:
        return None
    if isinstance(window, str):
        return window
    count = window.get("count")
    grain = window.get("granularity")
    if count is None or grain is None:
        return None
    return f"{count} {grain}" if count == 1 else f"{count} {grain}s"


def _metric_expr(rm: dict[str, Any], res: _Resolved) -> str | None:
    mtype = (rm.get("type") or "").lower()
    tp = rm.get("type_params") or {}
    if mtype == "simple":
        return _agg_expr(res.pairs[0][1])
    if mtype == "ratio":
        return f"{res.inputs[0]} / {res.inputs[1]}"
    if mtype == "derived":
        return tp.get("expr")
    if mtype == "cumulative":
        base = _agg_expr(res.pairs[0][1])
        ctp = tp.get("cumulative_type_params") or {}
        window = _window_text(ctp.get("window") or tp.get("window"))
        grain_to_date = ctp.get("grain_to_date") or tp.get("grain_to_date")
        if window:
            return f"{base} over a trailing {window} window"
        if grain_to_date:
            return f"{base} accumulated {grain_to_date} to date"
        return f"{base} accumulated over all time"
    if mtype == "conversion":
        ctp = tp.get("conversion_type_params") or {}
        base, conv = res.pairs[0][1], res.pairs[1][1]
        entity = ctp.get("entity")
        window = _window_text(ctp.get("window"))
        calc = (ctp.get("calculation") or "conversion_rate").lower()
        text = f"{calc} of {conv['name']} after {base['name']} per {entity}"
        return f"{text} within {window}" if window else text
    return None


def _time_dimensions(res: _Resolved) -> list[str]:
    out: list[str] = []
    for model, measure in res.pairs:
        td = measure.get("agg_time_dimension") or model.default_time_dimension
        if td:
            q = model.qualified(td)
            if q not in out:
                out.append(q)
    return out


def _metric_dimensions(
    name: str,
    raw_metrics: dict[str, dict[str, Any]],
    resolved: dict[str, _Resolved],
    reachable: dict[str, list[str]],
) -> list[str]:
    """Dimensions valid for a metric: intersection across everything it aggregates."""
    res = resolved[name]
    mtype = (raw_metrics[name].get("type") or "").lower()
    if mtype in ("ratio", "derived"):
        sets = [_metric_dimensions(i, raw_metrics, resolved, reachable) for i in res.inputs]
    else:
        sets = [reachable[model.name] for model, _ in res.pairs]
    if not sets:
        return []
    first = sets[0]
    common = set(first).intersection(*map(set, sets[1:]))
    return [d for d in first if d in common]


def _synonyms(meta: dict[str, Any]) -> list[str]:
    poly = meta.get("polyculture") or {}
    syn = poly.get("synonyms") if isinstance(poly, dict) else None
    if not syn:
        return []
    if isinstance(syn, str):
        return [syn]
    return [str(s) for s in syn]


def _metric_info(
    rm: dict[str, Any],
    resolved: dict[str, _Resolved],
    raw_metrics: dict[str, dict[str, Any]],
    reachable: dict[str, list[str]],
) -> MetricInfo:
    name = rm["name"]
    res = resolved[name]
    meta = dict(((rm.get("config") or {}).get("meta")) or {})
    time_dims = _time_dimensions(res)
    models: list[str] = []
    measures: list[str] = []
    relations: list[str] = []
    for model, measure in res.pairs:
        if model.name not in models:
            models.append(model.name)
        if measure["name"] not in measures:
            measures.append(measure["name"])
        if model.relation not in relations:
            relations.append(model.relation)
    meta["understory"] = {
        "semantic_models": models,
        "measures": measures,
        "time_dimensions": time_dims,
    }
    dims = _metric_dimensions(name, raw_metrics, resolved, reachable)
    return MetricInfo(
        name=name,
        label=rm.get("label"),
        description=rm.get("description"),
        type=(rm.get("type") or "").lower(),
        expr=_metric_expr(rm, res),
        filters=list(dict.fromkeys(res.filters)),
        inputs=list(res.inputs),
        dimensions=[METRIC_TIME, *dims],
        time_dimension=time_dims[0] if time_dims else None,
        relations=relations,
        synonyms=_synonyms(meta),
        meta=meta,
    )
