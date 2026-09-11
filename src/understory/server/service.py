"""The seven tools as plain Python, independent of MCP.

`Service` owns one tenant: its catalog, traps registry, semantic layer,
warehouse, telemetry writer, and session store. `mcp.py` wraps each method as
an MCP tool; the harness calls them directly. Keeping the logic here means the
eval harness and the chatbot exercise the same code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import date
from typing import Any

from understory.catalog.context import build_context
from understory.catalog.describe import describe_metric as _describe_metric
from understory.catalog.describe import list_metrics as _list_metrics
from understory.catalog.manifest import load_catalog
from understory.guard import guard_sql
from understory.protocols import CompileError, QueryError, SemanticLayer, Warehouse
from understory.semantic import make_semantic_layer
from understory.server.session import Session, SessionStore, review_answer
from understory.server.windows import apply_anchor
from understory.server.windows import describe as describe_window
from understory.telemetry import (
    AnswerLogged,
    ClarificationApplied,
    ClarificationReturned,
    QueryExecuted,
    Refused,
    TelemetryWriter,
    TextRecord,
    ToolCalled,
)
from understory.tenant import TenantConfig
from understory.traps.match import check as check_traps
from understory.traps.schema import Registry, load_registry
from understory.types import (
    AnswerReview,
    Catalog,
    MetricSpec,
    Provenance,
    Refusal,
    Result,
    Status,
    ToolResponse,
)
from understory.warehouse import make_warehouse

log = logging.getLogger(__name__)

_FRESHNESS_TTL_S = 300

HOW_TO_READ_QUERY = (
    "Report the numbers as returned. State every required_disclosure in your answer. "
    "If provenance.governed is false, say the answer came from ad hoc SQL rather than "
    "governed metrics. Call log_answer with your draft before replying."
)
HOW_TO_READ_CLARIFICATION = (
    "Do not answer yet. Present each clarification's options to the user verbatim as a "
    "short choice, then call query_metrics again with the same spec plus a "
    "clarifications entry {trap, choice} for each answer."
)
HOW_TO_READ_REFUSAL = (
    "Tell the user plainly that this cannot be answered from the governed data and why. "
    "Offer the suggestions if any. Do not guess a number."
)


class Service:
    def __init__(
        self,
        tenant: TenantConfig,
        *,
        warehouse: Warehouse | None = None,
        semantic: SemanticLayer | None = None,
        telemetry: TelemetryWriter | None = None,
        catalog: Catalog | None = None,
        registry: Registry | None = None,
    ) -> None:
        self.tenant = tenant
        self.semantic = semantic or make_semantic_layer(tenant.semantic_layer, tenant.manifest_path)
        self.warehouse = warehouse or make_warehouse(
            tenant.warehouse, schemas=tenant.sql.schemas, timeout_s=tenant.limits.timeout_s
        )
        self.catalog = catalog or load_catalog(tenant.manifest_path)
        self.registry = registry or load_registry(tenant.traps_path)
        self.telemetry = telemetry or TelemetryWriter(tenant.log, tenant.name)
        self.sessions = SessionStore()
        self._freshness: dict[str, tuple[float, date | None]] = {}

    def close(self) -> None:
        self.telemetry.close()
        close = getattr(self.warehouse, "close", None)
        if close:
            close()

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    def get_context(self, session: Session) -> str:
        t0 = time.monotonic()
        text = build_context(self.tenant, self.catalog, self._data_through_all())
        self._tool(session, "get_context", "resolved", t0)
        return text

    def list_metrics(self, session: Session) -> dict[str, Any]:
        t0 = time.monotonic()
        rows = _list_metrics(self.catalog)
        self._tool(session, "list_metrics", "resolved", t0)
        return {
            "metrics": rows,
            "conventions": (
                "Time windows end at the latest date with data, never today. "
                "Name metrics exactly as listed. Dimensions use entity__dimension names."
            ),
        }

    def describe_metric(self, session: Session, name: str) -> dict[str, Any]:
        t0 = time.monotonic()
        if name not in self.catalog.metrics:
            near = self.catalog.nearest_metrics(name)
            self._tool(session, "describe_metric", "invalid", t0)
            return {
                "status": Status.invalid,
                "message": f"No metric named '{name}'.",
                "suggestions": near,
            }
        out = _describe_metric(self.catalog, name)
        info = self.catalog.metrics[name]
        out["data_through"] = _iso(self._anchor_for([info.name]))
        self._tool(session, "describe_metric", "resolved", t0)
        return out

    def search_dimension_values(
        self, session: Session, dimension: str, query: str = ""
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        dim = self.catalog.dimensions.get(dimension)
        if dim is None:
            self._tool(session, "search_dimension_values", "invalid", t0)
            return {
                "status": Status.invalid,
                "message": f"No dimension named '{dimension}'.",
                "suggestions": self.catalog.nearest_dimensions(dimension),
            }
        if dim.type == "time":
            self._tool(session, "search_dimension_values", "invalid", t0)
            return {
                "status": Status.invalid,
                "message": f"'{dimension}' is a time dimension; use time.start and time.end.",
            }
        try:
            res = self.warehouse.dimension_values(
                dim.relation, dim.column, query, self.tenant.limits.dimension_values_limit
            )
        except QueryError as e:
            self._tool(session, "search_dimension_values", "error", t0)
            return {"status": Status.error, "message": str(e)}
        self._tool(session, "search_dimension_values", "resolved", t0)
        return {
            "status": Status.resolved,
            "dimension": dimension,
            "values": [{"value": r[0], "count": r[1]} for r in res.rows],
            "truncated": res.truncated,
        }

    # ------------------------------------------------------------------ #
    # Governed query
    # ------------------------------------------------------------------ #

    def query_metrics(self, session: Session, spec: MetricSpec | dict[str, Any]) -> ToolResponse:
        t0 = time.monotonic()
        if isinstance(spec, dict):
            try:
                spec = MetricSpec.model_validate(spec)
            except Exception as e:
                return self._refuse(session, t0, "invalid", f"Bad spec: {e}", spec=None)

        # 1. Validate against the catalog.
        problem = self._validate(spec)
        if problem is not None:
            return self._refuse(session, t0, "invalid", problem.message, spec, problem.suggestions)

        # 2. Traps.
        outcome = check_traps(
            spec,
            self.registry,
            self.catalog,
            max_clarifications=self.tenant.limits.max_clarifications,
        )
        for c in spec.clarifications:
            self._emit(session, ClarificationApplied, trap_id=c.trap, choice=c.choice)
        if outcome.refusal is not None:
            r = outcome.refusal
            return self._refuse(session, t0, r.reason, r.message, spec, r.suggestions, r.phrase)
        if outcome.clarifications:
            for c in outcome.clarifications:
                self._emit(
                    session,
                    ClarificationReturned,
                    trap_id=c.trap,
                    options=[o.id for o in c.options],
                )
            self._tool(session, "query_metrics", "needs_clarification", t0)
            self._text(session, uuid.uuid4().hex, spec=spec)
            return ToolResponse(
                status=Status.needs_clarification,
                clarifications=outcome.clarifications,
                required_disclosures=[d.text for d in outcome.disclosures],
                how_to_read=HOW_TO_READ_CLARIFICATION,
            )
        spec = outcome.spec
        disclosures = [d.text for d in outcome.disclosures]

        # 3. Time.
        anchor = self._anchor_for(spec.metrics)
        window = self._chosen_window(spec)
        applied_time = apply_anchor(spec.time, anchor, window)
        spec = spec.model_copy(update={"time": applied_time})
        window_text = describe_window(applied_time, anchor, window)
        if window_text and not any("anchored" in d for d in disclosures):
            disclosures.append(window_text)

        # 4. Compile and execute.
        try:
            compiled = self.semantic.compile(spec)
        except CompileError as e:
            return self._refuse(session, t0, "invalid", str(e), spec)
        try:
            result = self.warehouse.run(
                compiled.sql,
                timeout_s=self.tenant.limits.timeout_s,
                row_cap=spec.limit or self.tenant.limits.row_cap,
            )
        except QueryError as e:
            self._tool(session, "query_metrics", "error", t0)
            return ToolResponse(
                status=Status.error,
                refusal=Refusal(reason="invalid", message=str(e)),
                how_to_read=HOW_TO_READ_REFUSAL,
            )

        # 5. Remember, log, return.
        result_id = session.remember("query_metrics", result, spec.metrics)
        session.owe(disclosures)
        event_id = uuid.uuid4().hex
        self._emit(
            session,
            QueryExecuted,
            event_id=event_id,
            metrics=spec.metrics,
            dimensions=spec.group_by,
            sql_hash=compiled.sql_hash,
            spec_hash=compiled.spec_hash,
            row_count=result.row_count,
            governed=True,
            cache_hit=compiled.cache_hit,
            truncated=result.truncated,
            latency_ms=_ms(t0),
        )
        self._text(session, event_id, spec=spec, sql=compiled.sql)
        self._tool(session, "query_metrics", "resolved", t0)
        prov = Provenance(
            governed=True,
            metrics=spec.metrics,
            dimensions=spec.group_by,
            relations=sorted({r for m in spec.metrics for r in self.catalog.metrics[m].relations}),
            sql=compiled.sql if self.tenant.sql.include_sql_in_governed_provenance else None,
            sql_hash=compiled.sql_hash,
            spec_hash=compiled.spec_hash,
            data_through=anchor,
            applied_time=applied_time,
            cache_hit=compiled.cache_hit,
            semantic_layer=self.semantic.name,
        )
        return ToolResponse(
            status=Status.resolved,
            result=result,
            required_disclosures=disclosures,
            provenance=prov,
            result_id=result_id,
            how_to_read=HOW_TO_READ_QUERY,
        )

    # ------------------------------------------------------------------ #
    # Escape hatch
    # ------------------------------------------------------------------ #

    def run_sql(self, session: Session, sql: str, question: str | None = None) -> ToolResponse:
        t0 = time.monotonic()
        relations = sorted({r for m in self.catalog.metrics.values() for r in m.relations})
        g = guard_sql(
            sql,
            dialect=self.warehouse.dialect,
            scope=self.tenant.sql,
            catalog_relations=relations,
            row_cap=self.tenant.limits.row_cap,
        )
        if not g.ok:
            event_id = uuid.uuid4().hex
            self._emit(session, Refused, event_id=event_id, reason="sql_rejected", phrase=None)
            self._text(session, event_id, question=question, sql=sql)
            self._tool(session, "run_sql", "sql_rejected", t0)
            return ToolResponse(
                status=Status.sql_rejected,
                refusal=Refusal(reason="sql_rejected", message=g.reason or "rejected"),
                how_to_read=HOW_TO_READ_REFUSAL,
            )
        try:
            result = self.warehouse.run(
                g.sql, timeout_s=self.tenant.limits.timeout_s, row_cap=self.tenant.limits.row_cap
            )
        except QueryError as e:
            self._tool(session, "run_sql", "error", t0)
            self._text(session, uuid.uuid4().hex, question=question, sql=g.sql)
            return ToolResponse(
                status=Status.error,
                refusal=Refusal(reason="sql_rejected", message=str(e)),
                how_to_read=HOW_TO_READ_REFUSAL,
            )
        result_id = session.remember("run_sql", result)
        disclosure = (
            "This answer came from ad hoc SQL, not from governed metric definitions. "
            "Treat it as unverified."
        )
        session.owe([disclosure])
        sql_hash = hashlib.sha256(g.sql.encode()).hexdigest()[:16]
        event_id = uuid.uuid4().hex
        self._emit(
            session,
            QueryExecuted,
            event_id=event_id,
            metrics=[],
            dimensions=[],
            sql_hash=sql_hash,
            row_count=result.row_count,
            governed=False,
            truncated=result.truncated,
            latency_ms=_ms(t0),
        )
        self._text(session, event_id, question=question, sql=g.sql)
        self._tool(session, "run_sql", "resolved", t0)
        return ToolResponse(
            status=Status.resolved,
            result=result,
            required_disclosures=[disclosure],
            provenance=Provenance(
                governed=False, relations=g.relations, sql=g.sql, sql_hash=sql_hash
            ),
            result_id=result_id,
            how_to_read=HOW_TO_READ_QUERY,
        )

    # ------------------------------------------------------------------ #
    # Answer capture
    # ------------------------------------------------------------------ #

    def log_answer(self, session: Session, draft: str) -> AnswerReview:
        t0 = time.monotonic()
        review = review_answer(draft, session)
        event_id = uuid.uuid4().hex
        self._emit(
            session,
            AnswerLogged,
            event_id=event_id,
            numbers_checked=len(review.checked),
            numbers_unsourced=len(review.unsourced),
            disclosures_present=len(review.disclosures_present),
            disclosures_missing=len(review.disclosures_missing),
        )
        self._text(session, event_id, draft_answer=draft)
        self._tool(session, "log_answer", review.status, t0)
        return review

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _validate(self, spec: MetricSpec) -> Refusal | None:
        if not spec.metrics:
            return Refusal(reason="invalid", message="spec.metrics is empty")
        for m in spec.metrics:
            if m not in self.catalog.metrics:
                return Refusal(
                    reason="invalid",
                    message=f"No metric named '{m}'.",
                    suggestions=self.catalog.nearest_metrics(m),
                )
        allowed: set[str] | None = None
        for m in spec.metrics:
            dims = set(self.catalog.metrics[m].dimensions)
            allowed = dims if allowed is None else allowed & dims
        allowed = allowed or set()
        grains = {"day", "week", "month", "quarter", "year"}
        for d in list(spec.group_by) + [w.dimension for w in spec.where]:
            base, _, suffix = d.rpartition("__")
            if suffix in grains and base:
                d_base = base
            else:
                d_base = d
            if d_base not in allowed and d_base != "metric_time":
                near = [x for x in self.catalog.nearest_dimensions(d) if x in allowed][:3]
                return Refusal(
                    reason="invalid",
                    message=(
                        f"Dimension '{d}' is not available for {', '.join(spec.metrics)}. "
                        "Valid dimensions are listed by describe_metric."
                    ),
                    suggestions=near or sorted(allowed)[:5],
                )
        return None

    def _chosen_window(self, spec: MetricSpec) -> str | None:
        for trap in self.registry.conventions:
            if trap.name != "default_window":
                continue
            for c in spec.clarifications:
                if c.trap == trap.id:
                    return c.choice
            if trap.policy != "ask_if_absent" and spec.time.start is None and spec.time.end is None:
                return trap.value
        return None

    def _data_through(self, time_dimension: str | None) -> date | None:
        if time_dimension is None:
            return None
        now = time.monotonic()
        cached = self._freshness.get(time_dimension)
        if cached and now - cached[0] < _FRESHNESS_TTL_S:
            return cached[1]
        dim = self.catalog.dimensions.get(time_dimension)
        value: date | None = None
        if dim is not None:
            try:
                value = self.warehouse.latest_date(dim.relation, dim.column)
            except QueryError as e:
                log.warning("latest_date failed for %s: %s", time_dimension, e)
        self._freshness[time_dimension] = (now, value)
        return value

    def _anchor_for(self, metrics: list[str]) -> date | None:
        """Earliest latest-date across every time dimension the metrics read.

        A derived metric over orders and returns is only complete through the
        older of the two, so that is the anchor.
        """
        dims: list[str] = []
        for m in metrics:
            info = self.catalog.metrics[m]
            extra = info.meta.get("understory", {}).get("time_dimensions") or []
            for td in [info.time_dimension, *extra]:
                if td and td not in dims:
                    dims.append(td)
        dates = [d for d in (self._data_through(td) for td in dims) if d is not None]
        return min(dates) if dates else None

    def _data_through_all(self) -> dict[str, date]:
        out: dict[str, date] = {}
        for m in self.catalog.metrics.values():
            if m.time_dimension and m.time_dimension not in out:
                d = self._data_through(m.time_dimension)
                if d is not None:
                    out[m.time_dimension] = d
        return out

    def _refuse(
        self,
        session: Session,
        t0: float,
        reason: str,
        message: str,
        spec: MetricSpec | None,
        suggestions: list[str] | None = None,
        phrase: str | None = None,
    ) -> ToolResponse:
        event_id = uuid.uuid4().hex
        self._emit(session, Refused, event_id=event_id, reason=reason, phrase=phrase)
        if spec is not None:
            self._text(session, event_id, spec=spec)
        self._tool(session, "query_metrics", reason, t0)
        status = Status(reason) if reason in Status.__members__ else Status.invalid
        return ToolResponse(
            status=status,
            refusal=Refusal(
                reason=reason,  # type: ignore[arg-type]
                message=message,
                phrase=phrase,
                suggestions=suggestions or [],
            ),
            how_to_read=HOW_TO_READ_REFUSAL,
        )

    def _emit(self, session: Session, cls: type, **fields: Any) -> None:
        try:
            self.telemetry.emit(
                cls(
                    tenant=self.tenant.name,
                    user_hash=session.user_hash or "anonymous",
                    session_id=session.session_id,
                    **fields,
                )
            )
        except Exception as e:  # telemetry must never break a tool
            log.warning("telemetry emit failed: %s", e)

    def _tool(self, session: Session, tool: str, status: str, t0: float) -> None:
        self._emit(session, ToolCalled, tool=tool, status=status, latency_ms=_ms(t0))

    def _text(
        self,
        session: Session,
        event_id: str,
        *,
        spec: MetricSpec | None = None,
        question: str | None = None,
        sql: str | None = None,
        draft_answer: str | None = None,
    ) -> None:
        try:
            self.telemetry.emit_text(
                TextRecord(
                    event_id=event_id,
                    tenant=self.tenant.name,
                    question=question or (spec.question if spec else None),
                    spec_json=json.dumps(spec.model_dump(mode="json")) if spec else None,
                    sql=sql,
                    draft_answer=draft_answer,
                )
            )
        except Exception as e:
            log.warning("telemetry text failed: %s", e)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


__all__ = ["Service", "Result"]
