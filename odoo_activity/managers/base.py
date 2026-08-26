"""What every process manager has to answer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from odoo_activity.probes import LOCAL

if TYPE_CHECKING:
    from pathlib import Path

    from odoo_activity.host import Host
    from odoo_activity.probes import Instance


class Manager:
    """One process manager's answers about the instances it runs.

    Subclasses override only what their manager genuinely does differently.
    The defaults here are the least presumptuous ones -- read what the row
    already carries, run nothing -- so a manager that cannot do something
    says so rather than inheriting another's behaviour by accident.
    """

    name = ""

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        """Every Odoo instance this manager runs on `host`."""
        raise NotImplementedError

    def host_for(self, inst: Instance, host: Host = LOCAL) -> Host:
        """`host` narrowed to where this instance's processes and files
        actually live.

        This is the hook that keeps the interface small: return a `Host`
        that routes correctly and every shared argv-based probe in
        :mod:`odoo_activity.probes` works unchanged, with no per-manager
        branch of its own.
        """
        return host

    def pid(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """The master pid, or None when the manager cannot name one."""
        return inst.get("pid")

    def workdir(self, inst: Instance, host: Host = LOCAL) -> Path:
        """The directory this instance's relative paths resolve against."""
        from pathlib import Path

        return Path(inst.get("directory") or ".")

    def control(self, inst: Instance, action: str, host: Host = LOCAL) -> str:
        """Run start/stop/restart. "" on success, else what went wrong."""
        return f"{self.name or 'this manager'} cannot start or stop an instance"
