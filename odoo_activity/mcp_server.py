"""oa-mcp — MCP server exposing odoo-activity's read-only capacities.

POC, local use only. Every tool is a thin wrapper over
:mod:`odoo_activity.probes` — no logic lives here (see that module for the
actual system probes, shared with the TUI).
"""

from __future__ import annotations

from typing import Literal

import typer
from mcp.server.fastmcp import FastMCP

from odoo_activity import probes
from odoo_activity.probes import Instance, ProcRow

mcp = FastMCP("odoo-activity")
app = typer.Typer(add_completion=False)


def _with_status(inst: Instance) -> Instance:
    return {**inst, "status": probes.instance_status(inst)}


@mcp.tool()
def list_instances() -> list[Instance]:
    """All local Odoo instances (systemd --user, supervisor, odoo.sh), with
    each instance's status corrected for a live process behind an ambiguous
    manager-reported "stopped"."""
    return [_with_status(inst) for inst in probes.list_instances()]


@mcp.tool()
def get_instance(name: str) -> Instance | None:
    """A single instance by name (as reported by `list_instances`), or None
    if no instance with that name is currently known."""
    return next((inst for inst in list_instances() if inst["name"] == name), None)


@mcp.tool()
def list_processes(name: str) -> list[ProcRow]:
    """The instance's master process plus every descendant worker, or an
    empty list if the instance isn't found or has no live process."""
    inst = next((inst for inst in probes.list_instances() if inst["name"] == name), None)
    return probes.procs_of(inst) if inst else []


@app.callback(invoke_without_command=True)
def main(
    transport: Literal["stdio", "streamable-http"] = typer.Option(
        "stdio", "--transport", help="stdio (spawned by the client) or streamable-http (network server)"
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="streamable-http only"),
    port: int = typer.Option(8000, "--port", help="streamable-http only"),
) -> None:
    """oa-mcp — MCP server exposing odoo-activity's read-only capacities."""
    if transport == "streamable-http":
        mcp.settings.host = host
        mcp.settings.port = port

    mcp.run(transport=transport)


if __name__ == "__main__":
    app()
