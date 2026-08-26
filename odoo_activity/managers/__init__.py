"""Process managers: who finds an instance, and how to reach it.

An Odoo instance runs under systemd, supervisor, odoo.sh, docker compose, or
nothing at all. A manager owns everything that differs between those --
where instances are discovered, which pid is the master, what "restart"
means -- and answers in the shapes the panes already expect, so a tab never
learns which manager it is looking at.

The layering this sits in:

- :mod:`odoo_activity.probes` is the shared low-level probing (ps, cat,
  psql, the parsers). It knows nothing about managers, and nothing here
  imports back into it in the other direction.
- this package holds the per-manager knowledge *and* the dispatch functions
  the panes call, so `probes` never has to import upwards.
- the panes are the services: they ask for data and render it.

Most probes need no manager at all: `host_for` hands back a `Host` that
already routes to the right place (a container gets `docker exec` and
`is_local = False`), and the shared argv-based probes then work unchanged.
A manager overrides only what argv cannot express.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from odoo_activity import probes
from odoo_activity.managers.base import Manager
from odoo_activity.managers.docker import DockerManager
from odoo_activity.managers.local import LocalManager
from odoo_activity.managers.odoosh import OdooshManager
from odoo_activity.managers.supervisor import SupervisorManager
from odoo_activity.managers.systemd import SystemdManager
from odoo_activity.probes import (
    LOCAL,
    container_host,
    db_container_host,
    instance_pid,
    instance_workdir,
)

if TYPE_CHECKING:
    from odoo_activity.host import Host
    from odoo_activity.probes import Instance

# Discovery order is display order: the managers a Trobz box actually uses
# first, and `local` last -- it is the fallback for a process no manager
# claims, so it reads as a footnote to the list rather than its head.
MANAGERS: list[Manager] = [
    SystemdManager(),
    SupervisorManager(),
    OdooshManager(),
    DockerManager(),
    LocalManager(),
]

_BY_NAME = {manager.name: manager for manager in MANAGERS}


def manager_of(inst: Instance) -> Manager:
    """The manager that discovered `inst`.

    Falls back to `local` for a row from somewhere unexpected: its answers
    are the least presumptuous (read the row, run nothing), so an unknown
    manager degrades to showing what is already known rather than raising
    in the middle of a render.
    """
    return _BY_NAME.get(inst["manager"], _BY_NAME["local"])


def list_instances(host: Host = LOCAL) -> list[Instance]:
    """Every Odoo instance on `host`, from every manager.

    Each row carries its `manager` so later calls route back to the one that
    found it; two managers can even expose the same name (e.g. odoo-demo).
    """
    return [inst for manager in MANAGERS for inst in manager.instances(host)]


def instance_status(inst: Instance, host: Host = LOCAL) -> str:
    """The instance's corrected status: `running`, `stopped`, or a manager
    failure state (`failed`/`exited`/`fatal`).

    A manager may report "stopped" while a bare shell runs it, so a live
    process promotes an ambiguous *stopped* report to running. An explicit
    failure (systemd "failed", supervisor "exited"/"fatal") is authoritative
    even if a process serving the same db is alive -- `procs_of` matches by
    db name, not manager, so that process may belong to the *other*
    manager's instance of the same name/db.
    """
    if inst["status"] == "running":
        return "running"

    if inst["status"] == "stopped" and probes.procs_of(inst, host):
        return "running"

    return inst["status"]


def instance_action(inst: Instance, action: str, host: Host = LOCAL) -> str:
    """start/stop/restart `inst` through its own manager.

    Returns "" on success, else the controller's error output, so the UI can
    show why nothing happened instead of failing silently.
    """
    return manager_of(inst).control(inst, action, host)


# Aliases, not wrappers: `probes` dispatches these through `manager_of` too
# (it needs them itself, see `probes._manager_of`), and naming them here as
# well means a pane imports every instance-scoped call from one place rather
# than having to know which layer each happens to live in.
host_for = container_host
db_host_for = db_container_host

__all__ = [
    "MANAGERS",
    "Manager",
    "db_host_for",
    "host_for",
    "instance_action",
    "instance_pid",
    "instance_status",
    "instance_workdir",
    "list_instances",
    "manager_of",
]
