"""`understory ask` and `understory eval`: the harness from the command line.

`ask` is the demo path: one question through the agent, with the tool trace
printed so you can see which tools it reached for and what they said. `eval`
runs a tenant's golden set, prints the summary table and writes the JSON report
under `<tenant>/.evals/`.

`draft-realistic`, `draft-golden`, `fill` and `verify` build the golden sets
without a model: `draft-realistic` writes template questions from the seeded
ground truth, `draft-golden` writes them from the catalog and the traps
registry, `fill` runs every spec through the service once and records the
numbers it saw, and `verify` marks the snapshots a person has checked.

`eval` spends money, so it reads the key's remaining credit first and refuses a
set the balance cannot cover, prints the running total after each item, and
stops at the first out of credits error. `--concurrency` stays at 1 by default
for the same reason: twelve conversations in flight can drain a balance before
the first report lands.

`--model` repeats, and `--tiers` is shorthand for the default and the cheap
model, so one command scores the realistic set on both and prints a comparison
table under the two reports. `--byo` runs the connector surfaces only, which is
what a client's chatbot sees; run it alongside the default mode and report both.

`--id` picks items by name and `--repeat` runs each one several times, which is
how a prompt change is measured on the handful of items that carry the variance
before it is confirmed on the whole set. `--prompt` and `--no-enforce-check`
are the harness variants under test.
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
    model: list[str] = typer.Option(
        None, "--model", "-m", help="OpenRouter model id. Repeat to score several."
    ),
    tiers: bool = typer.Option(
        False, "--tiers", help="Score on the default model and the cheap one, then compare."
    ),
    byo: bool = typer.Option(
        False,
        "--byo",
        help="Connector surfaces only: instructions, tool descriptions, get_context.",
    ),
    deterministic: bool = typer.Option(
        False, "--deterministic", help="Run the specs through the service with no model."
    ),
    golden_set: str = typer.Option(
        "trap", "--set", "-s", help="Which golden set: 'trap' (questions.yml) or 'realistic'."
    ),
    limit: int = typer.Option(0, "--limit", "-n", help="Only the first N items. 0 means all."),
    ids: list[str] = typer.Option(None, "--id", help="Only these item ids. Repeatable."),
    repeat: int = typer.Option(1, "--repeat", "-r", help="Run each item this many times."),
    prompt: str = typer.Option(
        "harness", "--prompt", help="Harness system prompt: 'harness' or 'lean'."
    ),
    enforce_check: bool = typer.Option(
        True,
        "--enforce-check/--no-enforce-check",
        help="Run the closing check on the reply when the model did not. Off in BYO mode.",
    ),
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
    from understory.harness.agent import DEFAULT_MODEL, TIERS
    from understory.harness.deterministic import run_deterministic
    from understory.harness.evals import (
        BUDGET_PER_ITEM,
        BudgetError,
        EvalReport,
        compare,
        run_evals,
        write_report,
    )
    from understory.harness.golden import load_golden
    from understory.server.service import Service
    from understory.tenant import load_tenant

    cfg = load_tenant(tenant)
    path = cfg.golden_set_path(golden_set)
    items = load_golden(path)
    if not items:
        typer.echo(f"{cfg.name}: no golden questions at {path}")
        raise typer.Exit(1)
    if ids:
        known = {i.id for i in items}
        missing = [i for i in ids if i not in known]
        if missing:
            typer.echo(f"{cfg.name}: no such items: {missing}")
            raise typer.Exit(1)
        items = [i for i in items if i.id in set(ids)]
    if limit:
        items = items[:limit]
    models = list(TIERS) if tiers else (model or [DEFAULT_MODEL])

    reports: list[EvalReport] = []
    service = Service(cfg)
    try:
        if deterministic:
            reports.append(run_deterministic(service, items))
        else:
            for name in models:
                reports.append(
                    run_evals(
                        service,
                        items,
                        model=name,
                        concurrency=concurrency,
                        budget_per_item=(
                            budget_per_item if budget_per_item is not None else BUDGET_PER_ITEM
                        ),
                        force=force,
                        echo=typer.echo,
                        byo=byo,
                        prompt=prompt,
                        enforce_check=enforce_check and not byo,
                        repeat=repeat,
                    )
                )
    except BudgetError as e:
        typer.echo(f"refusing to start: {e}")
        raise typer.Exit(2) from e
    finally:
        service.close()

    for report in reports:
        typer.echo(report.markdown())
        if write:
            path = write_report(report, cfg.root)
            typer.echo(f"report: {path}")
    if len(reports) > 1:
        typer.echo(compare(reports))
    if any(report.summary()["failures"] for report in reports):
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


@app.command("draft-golden")
def draft_golden_command(
    tenant: Path = typer.Option(..., "--tenant", "-t", help="Tenant directory or tenant.yml."),
    out: Path = typer.Option(
        None, "--out", "-o", help="Where to write. Default golden/questions.yml."
    ),
    append: bool = typer.Option(
        False, "--append", help="Add items with new ids to an existing file; keep the rest."
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing file."),
    by_dimension: bool = typer.Option(
        True, help="Also draft one item per metric grouped by its first categorical dimension."
    ),
) -> None:
    """Draft a trap set from the catalog and the traps registry. No model, no numbers."""
    from understory.harness.authoring import append_items, draft_golden
    from understory.harness.realistic import data_window, write_items
    from understory.server.service import Service
    from understory.tenant import load_tenant

    cfg = load_tenant(tenant)
    target = out or cfg.golden_path
    if target.exists() and not (append or overwrite):
        typer.echo(f"{target} exists; pass --append to add to it or --overwrite to replace it")
        raise typer.Exit(1)
    service = Service(cfg)
    try:
        window = data_window(service.catalog, service.warehouse)
        items = draft_golden(
            service.catalog, service.registry, window=window, by_dimension=by_dimension
        )
    finally:
        service.close()
    if target.exists() and append:
        added = append_items(target, items)
        typer.echo(f"{cfg.name}: {len(added)} of {len(items)} drafted items were new -> {target}")
        return
    header = (
        f"{cfg.display_name} trap set, drafted from the catalog and the traps registry.\n"
        "Wording is a template until someone rewrites it. Run `understory fill` to\n"
        "snapshot the numbers, then `understory verify` on the ones a person checked."
    )
    write_items(target, items, header=header)
    catalog = sum(1 for i in items if i.kind == "catalog")
    typer.echo(f"{cfg.name}: {len(items)} items ({catalog} from the catalog) -> {target}")


@app.command("verify")
def verify_command(
    tenant: Path = typer.Option(..., "--tenant", "-t", help="Tenant directory or tenant.yml."),
    ids: list[str] = typer.Argument(None, help="Item ids to mark. None with --list to review."),
    golden_set: str = typer.Option("trap", "--set", "-s", help="'trap' or 'realistic'."),
    clear: bool = typer.Option(False, "--clear", help="Clear the flag instead of setting it."),
    list_items: bool = typer.Option(
        False, "--list", help="Print the unverified items with their numbers, and stop."
    ),
) -> None:
    """Mark snapshots a person has checked against a known report."""
    from understory.harness.authoring import set_verified
    from understory.harness.golden import load_golden
    from understory.tenant import load_tenant

    cfg = load_tenant(tenant)
    path = cfg.golden_set_path(golden_set)
    items = load_golden(path)
    if list_items or not ids:
        pending = [i for i in items if not i.verified and i.expected.numbers]
        done = sum(1 for i in items if i.verified)
        typer.echo(
            f"{cfg.name} {golden_set}: {done} verified, {len(pending)} with numbers to check"
        )
        for i in pending:
            typer.echo(f"  {i.id}: {i.question}")
            typer.echo(f"      {i.expected.metrics or ''} {i.expected.numbers}")
        if not ids:
            return
    known = {i.id for i in items}
    unknown = [i for i in ids if i not in known]
    if unknown:
        typer.echo(f"no such items: {unknown}")
        raise typer.Exit(1)
    found = set_verified(path, ids, value=not clear)
    typer.echo(f"{'cleared' if clear else 'verified'} {len(found)}: {', '.join(found)} -> {path}")


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
