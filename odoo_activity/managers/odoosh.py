"""odoo.sh builds.

One host is one build, which is why nothing here takes an instance name:
there is only ever the one, and the platform owns its lifecycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from odoo_activity.managers.base import Manager
from odoo_activity.probes import LOCAL, _odoosh_master_pid, odoosh_instances

if TYPE_CHECKING:
    from odoo_activity.host import Host
    from odoo_activity.probes import Instance


class OdooshManager(Manager):
    name = "odoosh"

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        return odoosh_instances(host)

    def pid(self, inst: Instance, host: Host = LOCAL) -> str | None:
        return _odoosh_master_pid(host)

    def workdir(self, inst: Instance, host: Host = LOCAL) -> Path:
        if host.is_local:
            return Path.home()

        return Path(host.run(["sh", "-c", "echo $HOME"]).stdout.strip() or "/root")

    def control(self, inst: Instance, action: str, host: Host = LOCAL) -> str:
        """odoo.sh has no separate start/stop -- sleep/wake is the platform's
        call, not ours; only a restart of the workers is exposed."""
        if action != "restart":
            return "start/stop not supported — odoo.sh handles sleep/wake on its own"

        # odoosh-restart takes one service at a time, unlike `supervisorctl
        # restart` which restarts everything for the instance in one call --
        # so restart both services it's equivalent to.
        for service in ("http", "cron"):
            try:
                out = host.run(["odoosh-restart", service])
            except FileNotFoundError:
                return "odoosh-restart not found on PATH"

            if out.returncode != 0:
                return out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"

        return ""
