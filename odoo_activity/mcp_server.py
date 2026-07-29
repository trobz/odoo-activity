"""oa-mcp — MCP server exposing odoo-activity's read-only capacities.

POC. Every tool is a thin wrapper over :mod:`odoo_activity.probes` — no
logic lives here (see that module for the actual system probes, shared with
the TUI). The server itself always runs locally; each tool takes the host to
probe, so one server answers for this box and any ssh target.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Annotated, Literal

import typer
from mcp.server.fastmcp import FastMCP

from odoo_activity import probes
from odoo_activity.host import Host
from odoo_activity.probes import Instance, ProcRow

mcp = FastMCP("odoo-activity")
app = typer.Typer(add_completion=False)
app_multi = typer.Typer(add_completion=False)


# oa-mcp and oa-mcp-multi are "normal vs risky" split is just whether
# --host-filter is passed at launch, not separate code paths.
_DEFAULT_HOST_FILE = Path.home() / ".ssh" / "config"
_host_filter: re.Pattern[str] | None = None
_host_file: Path = _DEFAULT_HOST_FILE


def _allowed(alias: str | None) -> bool:
    # local is always allowed; unset filter allows everything (odoo
    # dbfilter's "empty means no restriction" spirit). fullmatch, not
    # odoo's match: this gates real ssh access, a prefix match would let
    # "prod" filter through "prod-evil.example.com".
    return alias is None or _host_filter is None or bool(_host_filter.fullmatch(alias))


def _check_host(alias: str | None) -> None:
    if not _allowed(alias):
        msg = f"host {alias!r} rejected by --host-filter"
        raise ValueError(msg)


_WILDCARD = re.compile(r"[*?]")


def _ssh_config_aliases(path: Path) -> list[str]:
    """Literal (non-wildcard, non-negated) aliases from `Host` entries in an
    ssh config file. No `Include` following -- point --host-file at the
    file that actually lists them if it matters."""
    if not path.is_file():
        return []

    aliases = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        parts = line.split()
        if not parts or parts[0].lower() != "host":
            continue
        aliases.extend(p for p in parts[1:] if not p.startswith("!") and not _WILDCARD.search(p))

    return aliases


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
    _check_host(host)
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
    _check_host(host)
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
    _check_host(host)
    target = Host(alias=host, port=ssh_port)
    inst = next((i for i in _instances(target) if i["name"] == name), None)
    return probes.procs_of(inst, target) if inst else []


@mcp.tool()
def list_hosts() -> list[str]:
    """Host aliases available for the `host` argument of the other tools:
    literal `Host` entries from --host-file (default ~/.ssh/config, wildcard
    and negated patterns skipped), narrowed by --host-filter."""
    return [alias for alias in _ssh_config_aliases(_host_file) if _allowed(alias)]


@app.callback(invoke_without_command=True)
def main(
    transport: Literal["stdio", "streamable-http"] = typer.Option(
        "stdio", "--transport", help="stdio (spawned by the client) or streamable-http (network server)"
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="streamable-http only"),
    port: int = typer.Option(8000, "--port", help="streamable-http only"),
) -> None:
    """oa-mcp — MCP server exposing odoo-activity's read-only capacities.

    Single-host, every tool still takes its own `host` per call, unrestricted."""
    if transport == "streamable-http":
        mcp.settings.host = host
        mcp.settings.port = port

    mcp.run(transport=transport)


@app_multi.callback(invoke_without_command=True)
def main_multi(
    transport: Annotated[
        Literal["stdio", "streamable-http"],
        typer.Option("--transport", help="stdio (spawned by the client) or streamable-http (network server)"),
    ] = "stdio",
    host: Annotated[str, typer.Option("--host", help="streamable-http only")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="streamable-http only")] = 8000,
    host_filter: Annotated[
        str | None,
        typer.Option(
            "--host-filter",
            help="regex (odoo dbfilter-style); only ssh targets it fullmatches are reachable. Unset: no restriction.",
        ),
    ] = None,
    host_file: Annotated[
        Path,
        typer.Option("--host-file", help="ssh config to read literal Host aliases from, for list_hosts()."),
    ] = _DEFAULT_HOST_FILE,
) -> None:
    """oa-mcp-multi — same tools as oa-mcp, capped by --host-filter."""
    global _host_filter, _host_file
    if transport == "streamable-http":
        mcp.settings.host = host
        mcp.settings.port = port

    _host_filter = re.compile(host_filter) if host_filter else None
    _host_file = host_file

    mcp.run(transport=transport)


if __name__ == "__main__":
    # Stay pointing at single-host, dev-run default
    app()
