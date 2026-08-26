"""docker compose projects.

The manager that pays for this interface existing: its instances live in
another pid namespace and another filesystem, so `host_for` is what makes
every shared probe work against them without a branch of its own.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from odoo_activity import probes
from odoo_activity.managers.base import Manager
from odoo_activity.probes import LOCAL

if TYPE_CHECKING:
    from odoo_activity.host import Host
    from odoo_activity.probes import Instance, ProcRow, Worker

# doodba first, then the official image. An image puts the config where it
# likes, so there is no `<workdir>/config/` convention to walk -- a custom
# image names it on the command line instead, which `_config_file` handles
# before it ever gets here.
_CONFIG_PATHS = ("/opt/odoo/auto/odoo.conf", "/etc/odoo/odoo.conf")


class DockerManager(Manager):
    name = "docker"

    def instances(self, host: Host = LOCAL) -> list[Instance]:
        return probes.docker_instances(host)

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
        return probes._odoo_master_in(self.host_for(inst, host))

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
        return probes._docker_action(action, host, inst.get("workdir"))

    def config(self, inst: Instance, host: Host = LOCAL) -> tuple[Path | None, str | None]:
        """Read with `docker cp`, not `docker exec cat`: exec needs the
        container running, and a config is exactly what you want to read
        when an instance won't start (verified: with the container stopped,
        an exec probe reported no config at all).

        Path *and* contents in one go: locating the file and reading it are
        the same copy, and doing them separately meant four copies out of
        the container per `databases_of`.
        """
        at = self.host_for(inst, host)
        if at.container is None:
            return None, None

        for candidate in _CONFIG_PATHS:
            text = probes._read_out_of_container(at.container, candidate, at.on_box)
            if text is not None:
                return Path(candidate), text

        return None, None

    def config_file(self, inst: Instance, host: Host = LOCAL) -> Path | None:
        return self.config(inst, host)[0]

    def argv_settings(self, inst: Instance) -> str:
        """compose's `command:` is argv the same way a shell-run instance's
        is, and doodba puts real settings there (--workers, --dev)."""
        return inst.get("command", "")

    def logfile(self, inst: Instance, host: Host = LOCAL) -> Path | None:
        """None, always: an odoo image leaves `logfile` unset and writes to
        stdout, where docker keeps the stream itself."""
        return None

    def log_snapshot(self, inst: Instance, host: Host = LOCAL, lines: int = 200) -> str | None:
        # Both streams: odoo logs to stderr, docker keeps the two apart, and
        # a Logs tab showing only stdout would be empty on every instance.
        # Concatenated rather than interleaved -- there is no shell here to
        # `2>&1` with -- which is right for odoo (everything is on stderr)
        # and the reason the follow stream below merges them properly.
        out = host.run(["docker", "logs", "--tail", str(lines), inst["container"]])

        return probes._untty(out.stderr + out.stdout)

    def log_stream(self, inst: Instance, host: Host = LOCAL) -> subprocess.Popen | None:
        return host.popen(
            ["docker", "logs", "-f", "--tail", "0", inst["container"]],
            stderr=subprocess.STDOUT,
            text=False,
        )

    def version(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """`odoo --version` inside the container: odoo-addons-path is one of
        our own tools, installed on the box and not in the image -- and the
        layout it would inspect is inside the container anyway."""
        out = self.host_for(inst, host).run(["odoo", "--version"]).stdout
        found = re.search(r"(\d+\.\d+)", out)

        return found.group(1) if found else None

    def dump_stacks(self, inst: Instance, procs: list[ProcRow], host: Host = LOCAL) -> tuple[str, list[Worker]]:
        """The log is a stream, so the reader attaches before the signal --
        there is no offset to seek back to afterwards."""
        return probes._dump_via_stream(inst, procs, host)
