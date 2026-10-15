"""The MCP server: seven tools over one Service.

Every tool takes the SDK Context so it can find the per-connection session
and the authenticated subject. Tool logic lives in `service.py`; this module
only adapts arguments and return shapes.

`Service` is synchronous: a tool call can wait on the warehouse up to the
tenant's timeout, or on a cold `mf` compile. A tenant is one process, so each
call runs on a worker thread and the event loop stays free for everyone else.
The session is looked up on the loop first, where the request's headers and
subject are.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from pydantic import AnyHttpUrl

from understory.server.auth import current_subject, make_verifier
from understory.server.service import Service
from understory.server.session import Session
from understory.server.surfaces import TOOL_DESCRIPTIONS, connector_instructions
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


TOOL_THREADS = 16
"""Tool calls in flight at once. More than this wait their turn, off the loop."""

_pool = ThreadPoolExecutor(max_workers=TOOL_THREADS, thread_name_prefix="understory-tool")


async def _off_loop[T](fn: Callable[..., T], *args: Any) -> T:
    """`asyncio.to_thread` on our own pool; the default one is sized to the CPU count."""
    call = functools.partial(contextvars.copy_context().run, fn, *args)
    return await asyncio.get_running_loop().run_in_executor(_pool, call)


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
    instructions = connector_instructions(tenant)

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

    @mcp.tool(description=TOOL_DESCRIPTIONS["get_context"])
    async def get_context(ctx: Context) -> str:
        return await _off_loop(service.get_context, _session(service, ctx))

    @mcp.tool(description=TOOL_DESCRIPTIONS["list_metrics"])
    async def list_metrics(ctx: Context) -> dict[str, Any]:
        return await _off_loop(service.list_metrics, _session(service, ctx))

    @mcp.tool(description=TOOL_DESCRIPTIONS["describe_metric"])
    async def describe_metric(name: str, ctx: Context) -> dict[str, Any]:
        return await _off_loop(service.describe_metric, _session(service, ctx), name)

    @mcp.tool(description=TOOL_DESCRIPTIONS["search_dimension_values"])
    async def search_dimension_values(
        dimension: str, ctx: Context, query: str = ""
    ) -> dict[str, Any]:
        return await _off_loop(
            service.search_dimension_values, _session(service, ctx), dimension, query
        )

    @mcp.tool(description=TOOL_DESCRIPTIONS["query_metrics"])
    async def query_metrics(spec: MetricSpec, ctx: Context) -> dict[str, Any]:
        response = await _off_loop(service.query_metrics, _session(service, ctx), spec)
        return response.model_dump(mode="json")

    @mcp.tool(description=TOOL_DESCRIPTIONS["run_sql"])
    async def run_sql(sql: str, question: str, reason: str, ctx: Context) -> dict[str, Any]:
        response = await _off_loop(service.run_sql, _session(service, ctx), sql, question, reason)
        return response.model_dump(mode="json")

    @mcp.tool(description=TOOL_DESCRIPTIONS["log_answer"])
    async def log_answer(draft: str, ctx: Context) -> dict[str, Any]:
        review = await _off_loop(service.log_answer, _session(service, ctx), draft)
        return review.model_dump(mode="json")

    return mcp
