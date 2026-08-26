"""Directly-run instances: an odoo somebody started from a shell.

Also the fallback for a row whose manager is unrecognised, which is why its
answers are the least presumptuous ones -- report what the row already
carries, and run nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from odoo_activity import probes
from odoo_activity.managers.base import Manager
from odoo_activity.probes import LOCAL

if TYPE_CHECKING:
    from odoo_activity.host import Host
    from odoo_activity.probes import Instance


class LocalManager(Manager):
    name = "local"

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        return probes.local_instances(host)

    def pid(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """Straight off the row: there is no MainPID to re-ask for, and
        `list_instances` re-runs on a timer, so a restart arrives as a fresh
        row rather than being tracked here."""
        return inst.get("pid")

    def control(self, inst: Instance, action: str, host: Host = LOCAL) -> str:
        return "no process manager — a directly-run instance can't be started or stopped from here"

    def argv_settings(self, inst: Instance) -> str:
        """Its own command line is where a shell-run instance's settings are,
        whether or not it also has a config file."""
        return inst.get("command", "")

    def logfile(self, inst: Instance, host: Host = LOCAL) -> Path | None:
        """The config's `logfile`, else stdout when it was redirected to a
        file -- the closest thing to one for a runner that never set it."""
        return super().logfile(inst, host) or probes._redirected_stdout(inst, host)

    def databases(self, inst: Instance, host: Host = LOCAL) -> tuple[list[str], str | None]:
        """`-d`/`db_name` on the command line pins the instance to those
        databases; unpinned is genuinely multi-db, so fall back to asking
        the cluster which ones its role owns."""
        _, parser = probes.instance_config(inst, host)

        if pinned := probes._opt(parser, "db_name"):
            return [name.strip() for name in pinned.split(",") if name.strip()], probes._opt(parser, "db_port")

        return super().databases(inst, host)
