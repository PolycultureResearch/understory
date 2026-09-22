"""The prompt surfaces a bring-your-own chatbot sees, in one place.

A client's chatbot never reads the harness system prompt. It sees three things:
the connector instructions, the tool descriptions, and whatever `get_context`
returns. The MCP server publishes the first two from here, and the harness's
BYO mode builds its agent from the same strings, so an eval in that mode
measures the surfaces a real connector gets and nothing more.

These strings are versioned with the code and changed one at a time. Every
change runs both golden sets in both modes before and after (design 10.6).
"""

from __future__ import annotations

from understory.tenant import TenantConfig

TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_context": (
        "Business context for this company: what it does, what the metrics mean, "
        "conventions, data freshness, and which tables run_sql may touch. Call first."
    ),
    "list_metrics": "Every governed metric with its label, description, type, and synonyms.",
    "describe_metric": (
        "One metric in depth: definition, filters baked in, valid dimensions, "
        "data freshness, and example specs for query_metrics."
    ),
    "search_dimension_values": (
        "Find real values of a categorical dimension, e.g. which countries exist. "
        "Use before filtering so 'the West' becomes actual values."
    ),
    "query_metrics": (
        "Run a governed metric query. Send a spec: metrics (names from list_metrics), "
        "group_by (entity__dimension names, or metric_time), where clauses, time "
        "{grain, start, end}, the user's question, and any clarifications "
        "[{trap, choice}] answered earlier. Returns rows with provenance, or "
        "needs_clarification with options to show the user, or a refusal."
    ),
    "run_sql": (
        "Escape hatch: run one read-only SELECT against the allowed schemas when no "
        "governed metric answers the question. Pass the user's question and a one-line "
        "reason the governed metrics could not answer it; both are recorded as a gap for "
        "the data team. The answer must be labeled as ad hoc SQL and state the reason."
    ),
    "log_answer": (
        "Send your draft answer before replying. Checks that every number traces to a "
        "result from this session and that required disclosures are present."
    ),
}
"""What each tool says about itself to a connector. Keyed by tool name."""


def connector_instructions(tenant: TenantConfig) -> str:
    """The MCP server's `instructions` field: the tenant's own text, or the default."""
    return tenant.instructions or (
        f"Understory exposes {tenant.display_name}'s governed metrics. Call get_context "
        "first. Use query_metrics for numbers; present clarifications verbatim; include "
        "every required_disclosure; call log_answer with your draft before replying."
    )


__all__ = ["TOOL_DESCRIPTIONS", "connector_instructions"]
