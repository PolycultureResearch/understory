"""`understory serve`: run one tenant's MCP server."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from understory.cli import app


@app.command()
def serve(
    tenant: Path = typer.Option(..., "--tenant", "-t", help="Tenant directory or tenant.yml."),
    host: str = typer.Option("127.0.0.1", help="Bind address for HTTP."),
    port: int = typer.Option(8000, help="Port for HTTP."),
    transport: str = typer.Option("http", help="http (streamable HTTP at /mcp) or stdio."),
    log_level: str = typer.Option("INFO"),
) -> None:
    """Serve a tenant over MCP."""
    import asyncio

    from understory.server.mcp import build_server
    from understory.server.service import Service
    from understory.tenant import load_tenant

    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(name)s %(message)s")
    cfg = load_tenant(tenant)
    service = Service(cfg)
    server = build_server(service)
    typer.echo(f"understory: serving {cfg.display_name} ({cfg.name}) over {transport}")
    try:
        if transport == "stdio":
            asyncio.run(server.run_stdio_async())
        elif transport == "http":
            asyncio.run(server.run_streamable_http_async(host=host, port=port))
        else:
            raise typer.BadParameter("transport must be http or stdio")
    finally:
        service.close()


@app.command()
def check(
    tenant: Path = typer.Option(..., "--tenant", "-t", help="Tenant directory or tenant.yml."),
) -> None:
    """Load a tenant end to end: manifest, traps, warehouse, semantic layer. Exit 1 on failure."""
    from understory.server.service import Service
    from understory.tenant import load_tenant
    from understory.traps.compile import check_registry

    cfg = load_tenant(tenant)
    service = Service(cfg)
    try:
        problems = check_registry(service.registry, service.catalog)
        for p in problems:
            typer.echo(f"traps: {p}")
        through = service._data_through_all()
        typer.echo(f"metrics: {len(service.catalog.metrics)}")
        typer.echo(f"dimensions: {len(service.catalog.dimensions)}")
        for td, d in through.items():
            typer.echo(f"data through {td}: {d}")
        if problems:
            raise typer.Exit(1)
        typer.echo("ok")
    finally:
        service.close()
