"""The MCP layer over an in-memory client, against the retail tenant."""

from __future__ import annotations

import asyncio
import json
import threading

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
    built = build_server(svc)
    _SERVICES[id(built)] = svc
    yield built
    svc.close()


_SERVICES: dict[int, Service] = {}


def server_service(server) -> Service:
    return _SERVICES[id(server)]


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
            "metrics": ["gross_margin"],
            "group_by": ["order__country"],
            "time": {"grain": "quarter", "start": "2025-01-01", "end": "2025-03-31"},
            "question": "What was our margin in the West last quarter?",
        }
        first = _payload(await client.call_tool("query_metrics", {"spec": spec}))
        assert first["status"] == "needs_clarification", first
        trap = first["clarifications"][0]["trap"]
        options = {o["id"] for o in first["clarifications"][0]["options"]}
        assert {"gross_margin", "margin_rate"} <= options

        spec["clarifications"] = [{"trap": trap, "choice": "margin_rate"}]
        second = _payload(await client.call_tool("query_metrics", {"spec": spec}))
        assert second["status"] == "resolved", second
        assert second["provenance"]["metrics"] == ["margin_rate"]
        assert second["result"]["rows"]

        value = second["result"]["rows"][0][-1]
        review = _payload(
            await client.call_tool("log_answer", {"draft": f"Margin rate was {value} there."})
        )
        assert review["status"] == "missing_disclosures", review
        draft = f"Margin rate was {value} there. " + " ".join(second["required_disclosures"])
        review = _payload(await client.call_tool("log_answer", {"draft": draft}))
        assert review["status"] == "pass", review


async def test_the_published_spec_requires_the_question_and_offers_earlier(server):
    async with Client(server) as client:
        [tool] = [t for t in (await client.list_tools()).tools if t.name == "query_metrics"]
    schema = tool.input_schema
    spec = schema["properties"]["spec"]
    if "$ref" in spec:
        spec = schema["$defs"][spec["$ref"].rsplit("/", 1)[-1]]
    assert "question" in spec["required"]
    assert "earlier" in spec["properties"] and "earlier" not in spec["required"]
    assert "own words" in spec["properties"]["question"]["description"]


async def test_a_slow_tool_call_leaves_the_event_loop_free(server, monkeypatch):
    """A query at the timeout must not stall the tenant's other users."""
    service = server_service(server)
    release = threading.Event()
    started = threading.Event()
    real = service.list_metrics

    def slow(session):
        started.set()
        assert release.wait(10), "the loop never got to release the call"
        return real(session)

    monkeypatch.setattr(service, "list_metrics", slow)
    async with Client(server) as client:
        blocked = asyncio.create_task(client.call_tool("list_metrics", {}))
        while not started.is_set():
            await asyncio.sleep(0.01)
        # The slow call is parked on a worker thread; another tool answers meanwhile.
        ctx = await asyncio.wait_for(client.call_tool("get_context", {}), timeout=5)
        assert "Alpenglow" in ctx.content[0].text
        assert not blocked.done()
        release.set()
        assert _payload(await blocked)["metrics"]
