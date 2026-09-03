"""supervisor programs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from odoo_activity import probes
from odoo_activity.managers.base import Manager
from odoo_activity.managers.systemd import _run_controller
from odoo_activity.probes import LOCAL

if TYPE_CHECKING:
    from pathlib import Path

    from odoo_activity.host import Host
    from odoo_activity.probes import Instance


class SupervisorManager(Manager):
    name = "supervisor"
    order = 20

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        return probes.supervisor_instances(host)

    def pid(self, inst: Instance, host: Host = LOCAL) -> str | None:
        out = host.run(["supervisorctl", "pid", inst["name"]]).stdout.strip()

        return out if out.isdigit() else None

    def workdir(self, inst: Instance, host: Host = LOCAL) -> Path:
        from pathlib import Path

        return Path(inst.get("directory") or ".")

    def control(self, inst: Instance, action: str, host: Host = LOCAL) -> str:
        return _run_controller(["supervisorctl", action, inst["name"]], host)
