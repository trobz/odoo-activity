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

from functools import lru_cache
from typing import TYPE_CHECKING

from odoo_activity import plugins, probes
from odoo_activity.managers.base import Manager
from odoo_activity.managers.local import LocalManager
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


@lru_cache(maxsize=1)
def _installed() -> tuple[tuple[Manager, ...], tuple[str, ...]]:
    """Every installed manager, ordered, plus a message per failure.

    The five bundled ones register in this project's own pyproject, through
    the same entry point group a third-party manager would use: one loader
    path, and the shipped managers exercise the door everyone else comes
    through.

    Resolved on first use rather than at import, so a manager that imports
    this package back cannot deadlock the import graph. Cached because
    discovery walks the installed distributions, and the answer cannot
    change while the process runs.
    """
    found, failures = plugins.load(plugins.MANAGER_GROUP)

    if not found:
        # Entry points come from the *installed* metadata, not the source
        # tree, so an editable install whose dist-info predates them
        # discovers nothing while happily running this very code -- and
        # with no manager there is no instance list at all. Say so, and
        # keep the fallback so the app opens instead of tracebacking.
        failures.append(
            "no process managers found -- the installed odoo-activity metadata is stale; "
            "reinstall it (`uv sync`, or `uv tool install --force --editable .`)"
        )
        found = [LocalManager()]

    found.sort(key=lambda manager: (manager.order, manager.name))

    return tuple(found), tuple(failures)


def installed() -> list[Manager]:
    """Every installed manager, in display order."""
    return list(_installed()[0])


def failures() -> list[str]:
    """One message per manager that failed to load -- the app reports these
    rather than silently listing fewer instances than the box has."""
    return list(_installed()[1])


def manager_of(inst: Instance) -> Manager:
    """The manager that discovered `inst`.

    Falls back to `local` for a row from somewhere unexpected: its answers
    are the least presumptuous (read the row, run nothing), so an unknown
    manager degrades to showing what is already known rather than raising
    in the middle of a render.
    """
    by_name = {manager.name: manager for manager in installed()}

    return by_name.get(inst["manager"]) or by_name.get("local") or LocalManager()


def list_instances(host: Host = LOCAL) -> list[Instance]:
    """Every Odoo instance on `host`, from every manager.

    Each row carries its `manager` so later calls route back to the one that
    found it; two managers can even expose the same name (e.g. odoo-demo).
    """
    return [inst for manager in installed() for inst in manager.instances(host)]


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
    "Manager",
    "db_host_for",
    "failures",
    "host_for",
    "installed",
    "instance_action",
    "instance_pid",
    "instance_status",
    "instance_workdir",
    "list_instances",
    "manager_of",
]
