"""The eval harness: a Pydantic AI agent over the same Service the MCP server serves.

`agent` builds and runs the agent, `golden` loads a tenant's question set,
`evals` scores a run, and `deterministic` runs the same golden set with no model
at all, which is what CI uses.
"""

from __future__ import annotations

from understory.harness.agent import (
    DEFAULT_MODEL,
    SYSTEM_PROMPT,
    Turn,
    build_agent,
    run_question,
)
from understory.harness.deterministic import run_deterministic
from understory.harness.evals import (
    BudgetError,
    EvalReport,
    ItemScore,
    check_budget,
    key_status,
    run_evals,
    write_report,
)
from understory.harness.golden import Expectation, GoldenItem, load_golden

__all__ = [
    "DEFAULT_MODEL",
    "SYSTEM_PROMPT",
    "BudgetError",
    "EvalReport",
    "Expectation",
    "GoldenItem",
    "ItemScore",
    "Turn",
    "build_agent",
    "check_budget",
    "key_status",
    "load_golden",
    "run_deterministic",
    "run_evals",
    "run_question",
    "write_report",
]
