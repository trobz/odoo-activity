"""systemd --user units."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from odoo_activity.managers.base import Manager
from odoo_activity.probes import LOCAL, _systemd_workdir, systemd_instances

if TYPE_CHECKING:
    from pathlib import Path

    from odoo_activity.host import Host
    from odoo_activity.probes import Instance


class SystemdManager(Manager):
    name = "systemd"

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        return systemd_instances(host)

    def pid(self, inst: Instance, host: Host = LOCAL) -> str | None:
        out = host.run(["systemctl", "--user", "show", inst["name"], "-p", "MainPID"]).stdout
        found = re.search(r"MainPID=(\d+)", out)

        # 0 is systemd's way of saying "no process", not a pid
        return found.group(1) if found and found.group(1) != "0" else None

    def workdir(self, inst: Instance, host: Host = LOCAL) -> Path:
        return _systemd_workdir(inst["name"], host)

    def control(self, inst: Instance, action: str, host: Host = LOCAL) -> str:
        # synchronous; --user odoo units activate fast. Move to a worker
        # if a unit's start/restart ever blocks the UI.
        return _run_controller(["systemctl", "--user", action, inst["name"]], host)


def _run_controller(cmd: list[str], host: Host) -> str:
    """Run a start/stop/restart command, and return "" or why it failed.

    Shared with supervisor: both are one argv whose exit code is the answer,
    and both are worth reporting rather than failing silently.
    """
    try:
        out = host.run(cmd)
    except FileNotFoundError:
        return f"{cmd[0]} not found on PATH"

    if out.returncode == 0:
        return ""

    return out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"
