"""`understory` command line. Subcommands are registered by the modules that own them."""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, help="Understory: natural-language interface for analytics.")


@app.command()
def version() -> None:
    from understory import __version__

    typer.echo(__version__)


def _register() -> None:
    """Import modules that add subcommands. Each is optional until it exists."""
    for mod in ("understory.traps.cli", "understory.server.cli", "understory.harness.cli"):
        try:
            __import__(mod)
        except ImportError:
            pass


_register()
