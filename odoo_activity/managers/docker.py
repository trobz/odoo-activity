"""docker compose projects.

The manager that pays for this interface existing: its instances live in
another pid namespace and another filesystem, so `host_for` is what makes
every shared probe work against them without a branch of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from odoo_activity.managers.base import Manager
from odoo_activity.probes import LOCAL, _docker_action, _odoo_master_in, docker_instances

if TYPE_CHECKING:
    from odoo_activity.host import Host
    from odoo_activity.probes import Instance


class DockerManager(Manager):
    name = "docker"

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        return docker_instances(host)

    def host_for(self, inst: Instance, host: Host = LOCAL) -> Host:
        return host.in_container(inst.get("container"))

    def db_host_for(self, inst: Instance, host: Host = LOCAL) -> Host:
        """`host` narrowed to the instance's *postgres* container. Only the
        Top tab's backend list needs this: everything else reaches postgres
        over TCP (see `pg_target_of`), not by running commands next to it."""
        return host.in_container(inst.get("db_container"))

    def pid(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """compose has no MainPID to ask for, and the pid is the container's
        own -- so it comes from a ps inside it. Not cached on the row: a
        container that restarted keeps its name and gets a fresh pid, which
        a cached one would miss."""
        return _odoo_master_in(self.host_for(inst, host))

    def workdir(self, inst: Instance, host: Host = LOCAL) -> Path:
        """The container's own working directory, not the compose project
        directory on the host: this is the base every *path* here is
        resolved against (config, logfile, data_dir) and those all live
        inside. The project directory is `workdir` on the row, used only to
        run invoke/docker compose (see `control`).

        Read off the image rather than /proc/<pid>/cwd: one call instead of
        a ps plus a readlink, and it answers for a stopped container too,
        which has no process to ask.
        """
        out = host.on_box.run(["docker", "inspect", "-f", "{{.Config.WorkingDir}}", inst["container"]]).stdout

        return Path(out.strip() or "/")

    def control(self, inst: Instance, action: str, host: Host = LOCAL) -> str:
        return _docker_action(action, host, inst.get("workdir"))
