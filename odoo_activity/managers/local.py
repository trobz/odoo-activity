"""Directly-run instances: an odoo somebody started from a shell.

Also the fallback for a row whose manager is unrecognised, which is why its
answers are the least presumptuous ones -- report what the row already
carries, and run nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from odoo_activity.managers.base import Manager
from odoo_activity.probes import LOCAL, local_instances

if TYPE_CHECKING:
    from odoo_activity.host import Host
    from odoo_activity.probes import Instance


class LocalManager(Manager):
    name = "local"

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        return local_instances(host)

    def pid(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """Straight off the row: there is no MainPID to re-ask for, and
        `list_instances` re-runs on a timer, so a restart arrives as a fresh
        row rather than being tracked here."""
        return inst.get("pid")

    def control(self, inst: Instance, action: str, host: Host = LOCAL) -> str:
        return "no process manager — a directly-run instance can't be started or stopped from here"
