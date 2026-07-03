import typer

from odoo_activity.tui import OdooActivity

app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """odoo-activity — TUI for local Odoo instances."""
    if ctx.invoked_subcommand is None:
        OdooActivity().run()


if __name__ == "__main__":
    app()
