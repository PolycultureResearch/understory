"""The MCP server: seven tools over one Service.

Every tool takes the SDK Context so it can find the per-connection session
and the authenticated subject. Tool logic lives in `service.py`; this module
only adapts arguments and return shapes.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from pydantic import AnyHttpUrl

from understory.server.auth import current_subject, make_verifier
from understory.server.service import Service
from understory.server.session import Session
from understory.telemetry import user_hash
from understory.types import MetricSpec

TOOL_NAMES = (
    "get_context",
    "list_metrics",
    "describe_metric",
    "search_dimension_values",
    "query_metrics",
    "run_sql",
    "log_answer",
)


def _session(service: Service, ctx: Context) -> Session:
    """One Session per MCP connection.

    Streamable HTTP clients send Mcp-Session-Id on every request after
    initialization, so that header is the key. stdio and in-memory transports
    carry one connection per process, so a fixed key is right there.
    """
    headers = ctx.headers or {}
    key = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id") or "default"
    subject = current_subject()
    if subject:
        key = f"{subject}:{key}"
    uh = user_hash(subject, service.tenant.log.user_hash_secret) if subject else None
    return service.sessions.get(key, user_hash=uh)


def build_server(service: Service) -> MCPServer:
    tenant = service.tenant
    instructions = tenant.instructions or (
        f"Understory exposes {tenant.display_name}'s governed metrics. Call get_context "
        "first. Use query_metrics for numbers; present clarifications verbatim; include "
        "every required_disclosure; call log_answer with your draft before replying."
    )

    kwargs: dict[str, Any] = {}
    verifier = make_verifier(tenant.auth)
    if verifier is not None:
        kwargs["token_verifier"] = verifier
        kwargs["auth"] = AuthSettings(
            issuer_url=AnyHttpUrl(tenant.auth.issuer_url),
            resource_server_url=(
                AnyHttpUrl(tenant.auth.resource_url) if tenant.auth.resource_url else None
            ),
        )

    mcp = MCPServer(
        name=f"understory-{tenant.name}",
        title=f"Understory: {tenant.display_name}",
        instructions=instructions,
        **kwargs,
    )

    @mcp.tool(
        description=(
            "Business context for this company: what it does, what the metrics mean, "
            "conventions, data freshness, and which tables run_sql may touch. Call first."
        )
    )
    async def get_context(ctx: Context) -> str:
        return service.get_context(_session(service, ctx))

    @mcp.tool(description="Every governed metric with its label, description, type, and synonyms.")
    async def list_metrics(ctx: Context) -> dict[str, Any]:
        return service.list_metrics(_session(service, ctx))

    @mcp.tool(
        description=(
            "One metric in depth: definition, filters baked in, valid dimensions, "
            "data freshness, and example specs for query_metrics."
        )
    )
    async def describe_metric(name: str, ctx: Context) -> dict[str, Any]:
        return service.describe_metric(_session(service, ctx), name)

    @mcp.tool(
        description=(
            "Find real values of a categorical dimension, e.g. which countries exist. "
            "Use before filtering so 'the West' becomes actual values."
        )
    )
    async def search_dimension_values(
        dimension: str, ctx: Context, query: str = ""
    ) -> dict[str, Any]:
        return service.search_dimension_values(_session(service, ctx), dimension, query)

    @mcp.tool(
        description=(
            "Run a governed metric query. Send a spec: metrics (names from list_metrics), "
            "group_by (entity__dimension names, or metric_time), where clauses, time "
            "{grain, start, end}, the user's question, and any clarifications "
            "[{trap, choice}] answered earlier. Returns rows with provenance, or "
            "needs_clarification with options to show the user, or a refusal."
        )
    )
    async def query_metrics(spec: MetricSpec, ctx: Context) -> dict[str, Any]:
        return service.query_metrics(_session(service, ctx), spec).model_dump(mode="json")

    @mcp.tool(
        description=(
            "Escape hatch: run one read-only SELECT against the allowed schemas when no "
            "governed metric answers the question. The answer must be labeled as ad hoc SQL."
        )
    )
    async def run_sql(sql: str, ctx: Context, question: str | None = None) -> dict[str, Any]:
        return service.run_sql(_session(service, ctx), sql, question).model_dump(mode="json")

    @mcp.tool(
        description=(
            "Send your draft answer before replying. Checks that every number traces to a "
            "result from this session and that required disclosures are present."
        )
    )
    async def log_answer(draft: str, ctx: Context) -> dict[str, Any]:
        return service.log_answer(_session(service, ctx), draft).model_dump(mode="json")

    return mcp
