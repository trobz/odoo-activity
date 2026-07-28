"""oa-mcp — MCP server exposing odoo-activity's read-only capacities.

POC. Every tool is a thin wrapper over :mod:`odoo_activity.probes` — no
logic lives here (see that module for the actual system probes, shared with
the TUI). The server itself always runs locally; each tool takes the host to
probe, so one server answers for this box and any ssh target.
"""

from __future__ import annotations

import time
from typing import Literal

import typer
from mcp.server.fastmcp import FastMCP

from odoo_activity import probes
from odoo_activity.host import Host
from odoo_activity.probes import Instance, ProcRow

mcp = FastMCP("odoo-activity")
app = typer.Typer(add_completion=False)


# Discovery is 8 ssh round trips (~620ms remote) behind every tool. Only the
# sweep is cached; status is re-probed per call. uptime rides along, so it
# can be this stale.
_DISCOVERY_TTL = 30.0
_discovered: dict[tuple[str | None, int | None], tuple[float, list[Instance]]] = {}


def _instances(host: Host) -> list[Instance]:
    key = (host.alias, host.port)
    cached = _discovered.get(key)
    now = time.monotonic()

    if cached is None or now - cached[0] >= _DISCOVERY_TTL:
        _discovered[key] = (now, probes.list_instances(host))

    return _discovered[key][1]


def _with_status(inst: Instance, host: Host) -> Instance:
    return {**inst, "status": probes.instance_status(inst, host)}


@mcp.tool()
def list_instances(host: str | None = None, ssh_port: int | None = None) -> list[Instance]:
    """Every Odoo instance on `host` (systemd --user, supervisor, odoo.sh),
    with each instance's status corrected for a live process behind an
    ambiguous manager-reported "stopped".

    Args:
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    target = Host(alias=host, port=ssh_port)
    return [_with_status(inst, target) for inst in _instances(target)]


@mcp.tool()
def get_instance(name: str, host: str | None = None, ssh_port: int | None = None) -> Instance | None:
    """A single instance by name (as reported by `list_instances`), or None
    if no instance with that name is currently known.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    target = Host(alias=host, port=ssh_port)
    inst = next((i for i in _instances(target) if i["name"] == name), None)
    return _with_status(inst, target) if inst else None


@mcp.tool()
def list_processes(name: str, host: str | None = None, ssh_port: int | None = None) -> list[ProcRow]:
    """The instance's master process plus every descendant worker, or an
    empty list if the instance isn't found or has no live process.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    target = Host(alias=host, port=ssh_port)
    inst = next((i for i in _instances(target) if i["name"] == name), None)
    return probes.procs_of(inst, target) if inst else []


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
