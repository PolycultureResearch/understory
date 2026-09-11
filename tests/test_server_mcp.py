"""The MCP layer over an in-memory client, against the retail tenant."""

from __future__ import annotations

import json

import pytest
from mcp.client import Client

from understory.server.mcp import TOOL_NAMES, build_server
from understory.server.service import Service
from understory.telemetry import TelemetryWriter
from understory.tenant import LogConfig

pytestmark = [pytest.mark.fake_db, pytest.mark.metricflow]


@pytest.fixture(scope="module")
def server(alpenglow_db, tmp_path_factory):
    logdir = tmp_path_factory.mktemp("log")
    log = LogConfig(events_prefix=str(logdir / "e"), text_prefix=str(logdir / "t"))
    svc = Service(alpenglow_db, telemetry=TelemetryWriter(log, alpenglow_db.name))
    yield build_server(svc)
    svc.close()


def _payload(result) -> dict:
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_tools_listed(server):
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == set(TOOL_NAMES)


async def test_clarification_round_trip(server):
    async with Client(server) as client:
        ctx = await client.call_tool("get_context", {})
        assert "Alpenglow" in ctx.content[0].text

        spec = {
            "metrics": ["gross_revenue"],
            "group_by": ["order__country"],
            "time": {"grain": "quarter", "start": "2025-01-01", "end": "2025-03-31"},
            "question": "How were sales in the West last quarter?",
        }
        first = _payload(await client.call_tool("query_metrics", {"spec": spec}))
        assert first["status"] == "needs_clarification", first
        trap = first["clarifications"][0]["trap"]
        options = {o["id"] for o in first["clarifications"][0]["options"]}
        assert {"net_revenue", "gross_revenue"} <= options

        spec["clarifications"] = [{"trap": trap, "choice": "net_revenue"}]
        second = _payload(await client.call_tool("query_metrics", {"spec": spec}))
        assert second["status"] == "resolved", second
        assert second["provenance"]["metrics"] == ["net_revenue"]
        assert second["result"]["rows"]

        value = second["result"]["rows"][0][-1]
        review = _payload(
            await client.call_tool("log_answer", {"draft": f"Net revenue was {value} there."})
        )
        assert review["status"] == "pass", review
