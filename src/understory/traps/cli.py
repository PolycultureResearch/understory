"""`understory traps check <tenant_dir>`: the CI compile step for a traps registry."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from understory.cli import app

traps_app = typer.Typer(no_args_is_help=True, help="Traps registry: check a tenant's traps.yml.")


@traps_app.command("check")
def check_command(
    tenant_dir: Annotated[Path, typer.Argument(help="Tenant directory holding tenant.yml.")],
) -> None:
    """Check traps.yml against the tenant's semantic manifest. Exits 1 on any problem."""
    from understory.catalog.manifest import load_catalog
    from understory.tenant import load_tenant
    from understory.traps.compile import check_registry
    from understory.traps.schema import load_registry

    cfg = load_tenant(tenant_dir)
    registry = load_registry(cfg.traps_path)
    catalog = load_catalog(cfg.manifest_path)
    problems = check_registry(registry, catalog)
    if problems:
        typer.echo(f"{cfg.name}: {len(problems)} problem(s) in {cfg.traps_path}")
        for problem in problems:
            typer.echo(f"  - {problem}")
        raise typer.Exit(code=1)
    count = len(registry.trap_ids())
    typer.echo(f"{cfg.name}: traps registry clean ({count} entries)")


app.add_typer(traps_app, name="traps")
