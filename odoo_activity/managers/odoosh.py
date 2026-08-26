"""odoo.sh builds.

One host is one build, which is why nothing here takes an instance name:
there is only ever the one, and the platform owns its lifecycle.
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


class OdooshManager(Manager):
    name = "odoosh"
    order = 30

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        return probes.odoosh_instances(host)

    def pid(self, inst: Instance, host: Host = LOCAL) -> str | None:
        return probes._odoosh_master_pid(host)

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

    def config_file(self, inst: Instance, host: Host = LOCAL) -> Path | None:
        """The fixed `~/.config/odoo/odoo.conf` odoo.sh always writes."""
        path = self.workdir(inst, host) / ".config" / "odoo" / "odoo.conf"

        return path if host.is_file(path) else None

    def logfile(self, inst: Instance, host: Host = LOCAL) -> Path | None:
        """odoo.sh's fixed `~/logs/odoo.log` -- its config is sparse and
        carries no `logfile` key at all."""
        path = self.workdir(inst, host) / "logs" / "odoo.log"

        return path if host.is_file(path) else None

    def data_dir(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """odoo.sh's fixed `~/data` -- its config has no `data_dir` key,
        same as it has no `logfile` one."""
        return str(self.workdir(inst, host) / "data")

    def version(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """Straight off the row: odoo.sh's own `$ODOO_VERSION`, captured at
        discovery time."""
        return inst.get("version")

    def databases(self, inst: Instance, host: Host = LOCAL) -> tuple[list[str], str | None]:
        """A single env-provided database (`PGDATABASE`), captured at
        discovery: there is exactly one, so no role query to run."""
        return [inst["db"]], None
