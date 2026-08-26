"""What every process manager has to answer."""

from __future__ import annotations

import configparser
import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from odoo_activity import probes
from odoo_activity.probes import LOCAL

if TYPE_CHECKING:
    from odoo_activity.host import Host
    from odoo_activity.probes import Instance, ProcRow, Worker


def _config_names(instance_name: str) -> list[str]:
    """Config filenames to try, in order.

    A multi-node instance's name is suffixed `-NN` (e.g. `foo-01`), and its
    config is `odooNN.conf` rather than `odoo.conf` -- `server.conf` is never
    node-numbered, so it's only ever tried plain.
    """
    if found := re.search(r"-(\d+)$", instance_name):
        return [f"odoo{found.group(1)}.conf", "odoo.conf", "server.conf"]

    return ["odoo.conf", "server.conf"]


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
        return Path(inst.get("directory") or ".")

    def control(self, inst: Instance, action: str, host: Host = LOCAL) -> str:
        """Run start/stop/restart. "" on success, else what went wrong."""
        return f"{self.name or 'this manager'} cannot start or stop an instance"

    def config(self, inst: Instance, host: Host = LOCAL) -> tuple[Path | None, str | None]:
        """(path, contents) of the odoo config, or (None, None).

        Both together, deliberately: for a manager that has to copy the file
        out of a container to read it, locating and reading it are the same
        copy.
        """
        path = self.config_file(inst, host)

        return (path, host.read_text(path)) if path is not None else (None, None)

    def config_file(self, inst: Instance, host: Host = LOCAL) -> Path | None:
        """The first `<workdir>/config/` file matching this instance's name."""
        workdir = self.workdir(inst, host)

        for name in _config_names(inst["name"]):
            path = workdir / "config" / name
            if host.is_file(path):
                return path

        return None

    def argv_settings(self, inst: Instance) -> str:
        """The command line whose odoo options layer *over* the config file,
        for a manager where argv carries real settings. Empty when the file
        is the whole story."""
        return ""

    def logfile(self, inst: Instance, host: Host = LOCAL) -> Path | None:
        """The odoo logfile, from the `logfile` key of the parsed config.

        None means there is no *file*, which is not the same as no log: a
        manager that keeps the stream itself says so here and answers in
        `log_snapshot` instead.
        """
        workdir, parser = probes.instance_config(inst, host)
        logfile = probes._opt(parser, "logfile")
        if logfile is None:
            return None

        path = Path(logfile)

        return path if path.is_absolute() else workdir / path

    def log_snapshot(self, inst: Instance, host: Host = LOCAL, lines: int = 200) -> str | None:
        """The last `lines` of the log, or None when there is none to read."""
        path = self.logfile(inst, host)

        return None if path is None else probes.tail(path, lines, host)

    def log_stream(self, inst: Instance, host: Host = LOCAL) -> subprocess.Popen | None:
        """A child streaming *new* log output, or None when the caller should
        poll a local file instead. Callers must `.kill()` it.

        `-n 0` because the lines already on screen came from `log_snapshot`:
        without it every follow replays the whole file into the pane.
        """
        path = self.logfile(inst, host)
        if path is None or host.is_local:
            return None

        return host.popen(["tail", "-f", "-n", "0", str(path)], stderr=subprocess.DEVNULL, text=False)

    def data_dir(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """The instance's `data_dir`, from its odoo config."""
        _, parser = probes.instance_config(inst, host)

        return probes._opt(parser, "data_dir")

    def version(self, inst: Instance, host: Host = LOCAL) -> str | None:
        """The instance's Odoo version, via the `odoo-addons-path` CLI --
        layout and addons-path detection live there, not here."""
        try:
            out = host.run(["odoo-addons-path", str(self.workdir(inst, host)), "--verbose", "--format", "json"]).stdout
        except FileNotFoundError:
            return None

        try:
            return json.loads(out).get("version")
        except (json.JSONDecodeError, ValueError):
            return None

    def dump_stacks(self, inst: Instance, procs: list[ProcRow], host: Host = LOCAL) -> tuple[str, list[Worker]]:
        """SIGQUIT every worker and collect what it wrote.

        The file strategy: note the log's size first, then read only what
        arrives past that offset. A manager whose log is a stream has to
        attach a reader *before* signalling instead.
        """
        return probes._dump_via_logfile(inst, procs, host)

    def databases(self, inst: Instance, host: Host = LOCAL) -> tuple[list[str], str | None]:
        """(databases, postgres port) for this instance.

        Read off one parser rather than calling `db_port_of`, which would
        re-fetch and re-parse the config: cheap to duplicate locally, but
        each fetch is its own ssh round trip remotely.
        """
        _, parser = probes.instance_config(inst, host)
        port = probes._opt(parser, "db_port")
        # ODOO_ACTIVITY_DB_ROLE describes *this box's* cluster convention
        # (locally every db is owned by `openerp`), so it wins where set;
        # else the config names the role odoo connects as, and the
        # instance's own name is the last resort.
        role = probes.DB_ROLE or probes._opt(parser, "db_user") or inst["name"].removesuffix(".service")

        return probes.databases_by_role(role, port, host), port

    def pg_target(
        self, inst: Instance, host: Host = LOCAL, parser: configparser.RawConfigParser | None = None
    ) -> probes.PgTarget:
        """Where this instance's postgres is, as the db-tab probes need it.

        A port on the box's own cluster, for every manager whose instance
        shares that cluster.
        """
        return probes.PgTarget(port=probes.db_port_of(inst, host))
