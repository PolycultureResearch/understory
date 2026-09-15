"""`understory ask` and `understory eval`: the harness from the command line.

`ask` is the demo path: one question through the agent, with the tool trace
printed so you can see which tools it reached for and what they said. `eval`
runs a tenant's golden set, prints the summary table and writes the JSON report
under `<tenant>/.evals/`.

`draft-realistic` and `fill` build the realistic set without a model:
`draft-realistic` writes template questions from the seeded ground truth,
`fill` runs every spec through the service once and records the numbers it saw.

`eval` spends money, so it reads the key's remaining credit first and refuses a
set the balance cannot cover, prints the running total after each item, and
stops at the first out of credits error. `--concurrency` stays at 1 by default
for the same reason: twelve conversations in flight can drain a balance before
the first report lands.
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
            typer.echo(f"  usage: {_usage_line(turn.usage)}")
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
    golden_set: str = typer.Option(
        "trap", "--set", "-s", help="Which golden set: 'trap' (questions.yml) or 'realistic'."
    ),
    limit: int = typer.Option(0, "--limit", "-n", help="Only the first N items. 0 means all."),
    concurrency: int = typer.Option(
        1, "--concurrency", "-c", help="Items in flight at once. Keep it at 1 to watch the spend."
    ),
    budget_per_item: float = typer.Option(
        None,
        "--budget-per-item",
        help="USD to budget per item when checking the key's remaining credit.",
    ),
    force: bool = typer.Option(False, "--force", help="Start even when the credit looks short."),
    write: bool = typer.Option(True, help="Write the JSON report under <tenant>/.evals/."),
) -> None:
    """Run a tenant's golden set and print the summary."""
    from understory.harness.agent import DEFAULT_MODEL
    from understory.harness.deterministic import run_deterministic
    from understory.harness.evals import BUDGET_PER_ITEM, BudgetError, run_evals, write_report
    from understory.harness.golden import load_golden
    from understory.server.service import Service
    from understory.tenant import load_tenant

    cfg = load_tenant(tenant)
    path = cfg.golden_set_path(golden_set)
    items = load_golden(path)
    if not items:
        typer.echo(f"{cfg.name}: no golden questions at {path}")
        raise typer.Exit(1)
    if limit:
        items = items[:limit]

    service = Service(cfg)
    try:
        if deterministic:
            report = run_deterministic(service, items)
        else:
            report = run_evals(
                service,
                items,
                model=model or DEFAULT_MODEL,
                concurrency=concurrency,
                budget_per_item=budget_per_item if budget_per_item is not None else BUDGET_PER_ITEM,
                force=force,
                echo=typer.echo,
            )
    except BudgetError as e:
        typer.echo(f"refusing to start: {e}")
        raise typer.Exit(2) from e
    finally:
        service.close()

    typer.echo(report.markdown())
    if write:
        path = write_report(report, cfg.root)
        typer.echo(f"report: {path}")
    if report.summary()["failures"]:
        raise typer.Exit(1)


@app.command("draft-realistic")
def draft_realistic_command(
    tenant: Path = typer.Option(..., "--tenant", "-t", help="Tenant directory or tenant.yml."),
    out: Path = typer.Option(
        None, "--out", "-o", help="Where to write. Default golden/realistic.yml."
    ),
    quiet: int = typer.Option(6, "--quiet", help="How many quiet-month items to pad with."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing file."),
) -> None:
    """Draft a realistic set from the warehouse's seeded ground truth. No model, no numbers."""
    from understory.harness.realistic import (
        data_window,
        draft_realistic,
        read_ground_truth,
        write_items,
    )
    from understory.server.service import Service
    from understory.tenant import load_tenant

    cfg = load_tenant(tenant)
    target = out or cfg.realistic_path
    if target.exists() and not overwrite:
        typer.echo(f"{target} exists; pass --overwrite to replace it")
        raise typer.Exit(1)
    service = Service(cfg)
    try:
        events = read_ground_truth(service.warehouse)
        if not events:
            typer.echo(f"{cfg.name}: no ground truth in the warehouse; nothing to draft from")
            raise typer.Exit(1)
        window = data_window(service.catalog, service.warehouse)
        items = draft_realistic(service.catalog, events, window=window, quiet_items=quiet)
    finally:
        service.close()
    header = (
        f"{cfg.display_name} realistic set, drafted from meta.ground_truth.\n"
        "Wording is a template until someone rewrites it. Run `understory fill` to\n"
        "snapshot the numbers, then hand-check them and set verified where you did."
    )
    write_items(target, items, header=header)
    rate = sum(1 for e in events if e.kind == "rate")
    typer.echo(f"{cfg.name}: {len(items)} items from {rate} rate events -> {target}")


@app.command("fill")
def fill_command(
    tenant: Path = typer.Option(..., "--tenant", "-t", help="Tenant directory or tenant.yml."),
    golden_set: str = typer.Option("realistic", "--set", "-s", help="'trap' or 'realistic'."),
    write: bool = typer.Option(True, help="Write the statuses and numbers back into the file."),
) -> None:
    """Run every spec through the service once and record what it returned."""
    from understory.harness.golden import load_golden
    from understory.harness.realistic import fill_numbers, patch_expected
    from understory.server.service import Service
    from understory.tenant import load_tenant

    cfg = load_tenant(tenant)
    path = cfg.golden_set_path(golden_set)
    items = load_golden(path)
    if not items:
        typer.echo(f"{cfg.name}: nothing at {path}")
        raise typer.Exit(1)
    service = Service(cfg)
    try:
        observations = fill_numbers(service, items)
    finally:
        service.close()
    by_id = {item.id: item for item in items}
    for obs in observations:
        mark = "MISMATCH " if obs.mismatch else ""
        typer.echo(f"{obs.id}: {mark}{obs.status} {obs.metrics or ''} {obs.numbers or ''}")
        for text in obs.disclosures:
            typer.echo(f"    ~ {text[:200]}")
        if obs.clarifications:
            typer.echo(f"    ? {obs.clarifications}")
        if obs.error:
            typer.echo(f"    ! {obs.error[:200]}")
        if obs.mismatch and not obs.error:
            typer.echo(f"    expected {by_id[obs.id].expected.status}; left unfilled")
    if write:
        patch_expected(path, items)
        typer.echo(f"wrote {path}")
    mismatched = [o.id for o in observations if o.mismatch]
    if mismatched:
        typer.echo(f"{len(mismatched)} items did not do what they expect: {mismatched}")
        raise typer.Exit(1)


def _usage_line(usage: dict[str, float]) -> str:
    """One line of tokens and money, cache reads included so caching is visible."""
    parts = [
        f"in={int(usage.get('input_tokens', 0)):,}",
        f"out={int(usage.get('output_tokens', 0)):,}",
        f"cache_read={int(usage.get('cache_read_tokens', 0)):,}",
        f"cache_write={int(usage.get('cache_write_tokens', 0)):,}",
        f"requests={int(usage.get('requests', 0))}",
    ]
    cost = usage.get("cost")
    if cost:
        parts.append(f"cost=${cost:.5f} ({cost * 100:.2f}c)")
    return " ".join(parts)


def _parse_answers(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        trap, sep, choice = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"--answer must be trap=choice, got {pair!r}")
        out[trap.strip()] = choice.strip()
    return out
