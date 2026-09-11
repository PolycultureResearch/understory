"""`understory ask` and `understory eval`: the harness from the command line.

`ask` is the demo path: one question through the agent, with the tool trace
printed so you can see which tools it reached for and what they said. `eval`
runs a tenant's golden set, prints the summary table and writes the JSON report
under `<tenant>/.evals/`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from understory.cli import app

harness_help = "Harness: ask one question or run a tenant's golden set."


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to ask."),
    tenant: Path = typer.Option(..., "--tenant", "-t", help="Tenant directory or tenant.yml."),
    model: str = typer.Option(None, "--model", "-m", help="OpenRouter model id."),
    answer: list[str] = typer.Option(
        None,
        "--answer",
        "-a",
        help="Clarification answer as trap=choice. Repeatable.",
    ),
    trace: bool = typer.Option(True, help="Print the tool trace."),
) -> None:
    """Ask one question through the harness agent."""
    from understory.harness.agent import DEFAULT_MODEL, run_question
    from understory.server.service import Service
    from understory.tenant import load_tenant

    cfg = load_tenant(tenant)
    answers = _parse_answers(answer)
    service = Service(cfg)
    try:
        turn = run_question(
            service,
            question,
            model=model or DEFAULT_MODEL,
            session_key="cli",
            clarification_answers=answers or None,
        )
    finally:
        service.close()

    if trace:
        typer.echo(f"--- {cfg.display_name} / {model or DEFAULT_MODEL}")
        for call in turn.tool_calls:
            args = ", ".join(f"{k}={v}" for k, v in call.args.items())
            typer.echo(f"  {call.name}({args}) -> {call.status}")
        for c in turn.clarifications:
            typer.echo(f"  asked {c.trap}: {', '.join(c.options)}")
        if turn.log_answer is not None:
            unsourced = [n.number for n in turn.log_answer.unsourced]
            typer.echo(f"  log_answer: {turn.log_answer.status} unsourced={unsourced}")
        if turn.usage:
            typer.echo(f"  usage: {turn.usage}")
        typer.echo("---")
    if turn.error:
        typer.echo(f"error: {turn.error}")
        raise typer.Exit(1)
    typer.echo(turn.answer)


@app.command("eval")
def eval_command(
    tenant: Path = typer.Option(..., "--tenant", "-t", help="Tenant directory or tenant.yml."),
    model: str = typer.Option(None, "--model", "-m", help="OpenRouter model id."),
    deterministic: bool = typer.Option(
        False, "--deterministic", help="Run the specs through the service with no model."
    ),
    limit: int = typer.Option(0, "--limit", "-n", help="Only the first N items. 0 means all."),
    concurrency: int = typer.Option(1, "--concurrency", "-c", help="Items in flight at once."),
    write: bool = typer.Option(True, help="Write the JSON report under <tenant>/.evals/."),
) -> None:
    """Run a tenant's golden set and print the summary."""
    from understory.harness.agent import DEFAULT_MODEL
    from understory.harness.deterministic import run_deterministic
    from understory.harness.evals import run_evals, write_report
    from understory.harness.golden import load_golden
    from understory.server.service import Service
    from understory.tenant import load_tenant

    cfg = load_tenant(tenant)
    items = load_golden(cfg.golden_path)
    if not items:
        typer.echo(f"{cfg.name}: no golden questions at {cfg.golden_path}")
        raise typer.Exit(1)
    if limit:
        items = items[:limit]

    service = Service(cfg)
    try:
        if deterministic:
            report = run_deterministic(service, items)
        else:
            report = run_evals(
                service, items, model=model or DEFAULT_MODEL, concurrency=concurrency
            )
    finally:
        service.close()

    typer.echo(report.markdown())
    if write:
        path = write_report(report, cfg.root)
        typer.echo(f"report: {path}")
    if report.summary()["failures"]:
        raise typer.Exit(1)


def _parse_answers(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        trap, sep, choice = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"--answer must be trap=choice, got {pair!r}")
        out[trap.strip()] = choice.strip()
    return out
