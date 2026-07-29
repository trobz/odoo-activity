"""System probes for odoo-activity — pure data, no TUI.

Everything the panes read about the host, its Odoo instances and their
databases lives here so it stays testable without spinning up Textual.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import os
import platform
import re
import select
import signal
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

# typing_extensions, not typing: pydantic (via FastMCP, see mcp_server.py)
# can't build a schema from typing.TypedDict on Python < 3.12.
from typing_extensions import TypedDict

from odoo_activity.host import LOCAL, Host

CLK_TCK = os.sysconf("SC_CLK_TCK")

# Some envs (like odoo.sh) export PATH with an unexpanded `~` (e.g., `~/.local/bin`).
# While shells auto-expand this, os.execvp / subprocess.run treat it as a literal
# string, making those binaries invisible. Expand it manually for this process tree.
os.environ["PATH"] = os.pathsep.join(os.path.expanduser(p) for p in os.environ.get("PATH", "").split(os.pathsep))


def _parse_uptime(text: str) -> float:
    return float(text.split()[0])


def read_uptime() -> float:
    """System uptime in seconds, from /proc/uptime."""
    with open("/proc/uptime") as f:
        return _parse_uptime(f.read())


def _parse_loadavg(text: str) -> tuple[float, float, float]:
    one, five, fifteen = text.split()[:3]
    return float(one), float(five), float(fifteen)


def read_loadavg() -> tuple[float, float, float]:
    """1/5/15-minute load averages, from /proc/loadavg."""
    return _parse_loadavg(Path("/proc/loadavg").read_text())


def format_duration(seconds: float) -> str:
    """`H:MM:SS`, or `<D>d HH:MM:SS` past a day."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{hours}:{minutes:02d}:{secs:02d}"


def _parse_cpu_times(line: str) -> tuple[int, int]:
    vals = [int(x) for x in line.split()[1:]]
    idle = vals[3] + vals[4]  # idle + iowait
    return sum(vals), idle


def read_cpu_times() -> tuple[int, int]:
    """Return (total, idle) jiffies from /proc/stat."""
    with open("/proc/stat") as f:
        return _parse_cpu_times(f.readline())


def _parse_mem(text: str) -> tuple[float, float]:
    info: dict[str, int] = {}
    for line in text.splitlines():
        key, rest = line.split(":", 1)
        info[key] = int(rest.split()[0])  # kB

    mem_pct = (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"] * 100
    swap_total = info["SwapTotal"]
    swap_pct = (swap_total - info["SwapFree"]) / swap_total * 100 if swap_total else 0.0

    return mem_pct, swap_pct


def read_mem() -> tuple[float, float]:
    """Return (mem_used_pct, swap_used_pct) from /proc/meminfo."""
    with open("/proc/meminfo") as f:
        return _parse_mem(f.read())


def read_host_stats(
    host: Host = LOCAL,
) -> tuple[tuple[int, int], tuple[float, float], tuple[float, float, float], float] | None:
    """(cpu_times, mem_pcts, loadavg, uptime) — the 4 separate local /proc
    reads for a local host, or one batched round trip for a remote one.

    None on a failed/timed-out remote round trip (a bad connection, not a
    parse bug) — this runs on a per-second timer forever, so the caller
    should just skip that tick's update rather than treat it as fatal.
    """
    if host.is_local:
        return read_cpu_times(), read_mem(), read_loadavg(), read_uptime()

    script = "head -1 /proc/stat; echo ---; cat /proc/meminfo; echo ---; cat /proc/loadavg; echo ---; cat /proc/uptime"
    result = host.run(["sh", "-c", script])
    if result.returncode != 0:
        return None

    parts = result.stdout.split("---\n", 3)
    if len(parts) != 4:
        return None

    stat_txt, mem_txt, load_txt, uptime_txt = parts
    return _parse_cpu_times(stat_txt), _parse_mem(mem_txt), _parse_loadavg(load_txt), _parse_uptime(uptime_txt)


_SUPERVISOR_STATUS = {
    "RUNNING": "running",
    "STARTING": "running",
    "STOPPED": "stopped",
    "STOPPING": "stopped",
    "UNKNOWN": "stopped",
    "BACKOFF": "fatal",
    "EXITED": "exited",
    "FATAL": "fatal",
}

_SYSTEMD_STATUS = {"active": "running", "failed": "failed"}


def _is_odoo(*text: str) -> bool:
    """True if any hint names an Odoo instance (odoo or the legacy openerp)."""
    blob = " ".join(text).lower()
    return "odoo" in blob or "openerp" in blob


def list_instances(host: Host = LOCAL) -> list[Instance]:
    """All Odoo instances on `host`, from systemd --user, supervisor and
    odoo.sh.

    Each row carries its `manager` so actions route to the right controller;
    managers can even expose the same name (e.g. odoo-demo).
    """
    return systemd_instances(host) + supervisor_instances(host) + odoosh_instances(host)


def instance_status(inst: Instance, host: Host = LOCAL) -> str:
    """The instance's corrected status: `running`, `stopped`, or a manager
    failure state (`failed`/`exited`/`fatal`).

    A manager may report "stopped" while a bare shell runs it, so a live
    process promotes an ambiguous *stopped* report to running. An explicit
    failure (systemd "failed", supervisor "exited"/"fatal") is authoritative
    even if a process serving the same db is alive — `procs_of` matches by
    db name, not manager, so that process may belong to the *other*
    manager's instance of the same name/db (see `list_instances`).
    """
    if inst["status"] == "running":
        return "running"
    if inst["status"] == "stopped" and procs_of(inst, host):
        return "running"
    return inst["status"]


def systemd_instances(host: Host = LOCAL) -> list[Instance]:
    """Odoo instances from systemd --user units.

    Uses list-unit-files (catches stopped units, which list-units hides) then
    one batched `show` to read Description + state for each. Matches the unit
    name or Description so name-convention units (openerp-*.service) are caught.
    """
    # --user only; add system-wide (`systemctl` without --user) when a
    # host needs it.
    try:
        files = host.run([
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service",
            "--no-legend",
            "--plain",
            "--no-pager",
        ]).stdout
    except FileNotFoundError:
        return []

    # drop template units (foo@.service) — `show` errors out on them
    units = [tok for tok in files.split() if tok.endswith(".service") and not tok.endswith("@.service")]
    if not units:
        return []

    show_cmd = [
        "systemctl",
        "--user",
        "show",
        *units,
        "-p",
        "Id",
        "-p",
        "Description",
        "-p",
        "ActiveState",
        "-p",
        "ActiveEnterTimestampMonotonic",
        "-p",
        "ActiveEnterTimestamp",
    ]
    if not host.is_local:
        # force a parseable weekday regardless of the remote's locale --
        # _systemd_active_secs's remote branch parses ActiveEnterTimestamp
        show_cmd = ["env", "LC_ALL=C", *show_cmd]
    out = host.run(show_cmd).stdout
    instances: list[Instance] = []

    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = props.get("Id", "")
        if not _is_odoo(name, props.get("Description", "")):
            continue

        status = _SYSTEMD_STATUS.get(props.get("ActiveState", ""), "stopped")
        uptime = "-"
        if status == "running" and (secs := _systemd_active_secs(props, host)) is not None:
            uptime = format_duration(secs)

        instances.append({"name": name, "status": status, "uptime": uptime, "manager": "systemd"})

    return instances


def _systemd_active_secs(props: dict[str, str], host: Host) -> float | None:
    """Seconds since a systemd unit's ActiveEnterTimestamp{,Monotonic}.

    Local: diffs the Monotonic property (microseconds since boot,
    CLOCK_MONOTONIC) against our own clock — excludes suspend time, so a
    resumed laptop doesn't overstate uptime by its sleep duration.

    Remote: CLOCK_MONOTONIC isn't queryable over ssh without another round
    trip, and /proc/uptime (CLOCK_BOOTTIME) isn't a safe stand-in for it —
    proven wrong against a real host where the two had diverged by months
    (a negative uptime). Diffs the wall-clock property against our own wall
    clock instead; NTP-synced closely enough for a rough uptime display.
    """
    if host.is_local:
        entered = int(props.get("ActiveEnterTimestampMonotonic", "0") or 0)
        if not entered:
            return None
        return time.clock_gettime(time.CLOCK_MONOTONIC) - entered / 1_000_000

    ts = props.get("ActiveEnterTimestamp", "")
    if not ts:
        return None
    # systemd emits a bare 2-digit UTC offset ("+07") when the minutes are
    # :00, and "UTC" literally for zero offset -- %z needs a 4-digit one.
    ts = re.sub(r" UTC$", " +0000", ts)
    ts = re.sub(r"([+-]\d{2})$", r"\g<1>00", ts)
    try:
        entered_at = datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    return time.time() - entered_at.timestamp()


# supervisor programs are declared one-per-file here on servers; the
# [program:x] section carries `directory=` (the instance's odoo dir).
# Below is the standard path on the server.
SUPERVISOR_CONFD = Path("/opt/openerp/supervisor/conf.d")


def supervisor_instances(host: Host = LOCAL) -> list[Instance]:
    """Odoo instances under supervisor.

    Names + `directory`/`command` come from the conf.d programs; running state
    comes from `supervisorctl status`. Works with either source alone — a host
    without the conf.d layout still lists what supervisorctl reports.
    """
    states = _supervisor_states(host)
    confs = _supervisor_confs(host)
    instances: list[Instance] = []

    for name in sorted(set(states) | set(confs)):
        if not _is_odoo(name):
            continue

        conf = confs.get(name, {})
        st = states.get(name, {"status": "stopped", "uptime": "-"})
        instances.append({
            "name": name,
            "status": st["status"],
            "uptime": st["uptime"],
            "manager": "supervisor",
            "command": conf.get("command", ""),
            "directory": conf.get("directory", ""),
        })

    return instances


def _supervisor_states(host: Host = LOCAL) -> dict[str, dict[str, str]]:
    """program -> {status, uptime} from `supervisorctl status` (skips the
    pkg_resources banner and any non-status lines). `uptime` is supervisor's
    own `H:MM:SS`/`D:HH:MM:SS` text, lifted straight out of the status line —
    only RUNNING/STARTING programs carry one. Returns {} if supervisor isn't
    installed on this host."""
    try:
        out = host.run(["supervisorctl", "status"]).stdout
    except FileNotFoundError:
        return {}

    states = {}

    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 2 or parts[1] not in _SUPERVISOR_STATUS:
            continue

        rest = parts[2] if len(parts) > 2 else ""
        uptime = rest.rsplit("uptime", 1)[1].strip(" ,") if "uptime" in rest else "-"
        states[parts[0]] = {"status": _SUPERVISOR_STATUS[parts[1]], "uptime": uptime}

    return states


def _supervisor_confs(host: Host = LOCAL) -> dict[str, dict[str, str]]:
    """program -> {command, directory} parsed from SUPERVISOR_CONFD/*.conf."""
    confs = {}

    for path in host.glob(str(SUPERVISOR_CONFD / "*.conf")):
        parser = configparser.RawConfigParser(strict=False)  # supervisor uses %
        try:
            parser.read_string(host.read_text(path))
        except configparser.Error:
            continue

        section = next((s for s in parser.sections() if s.startswith("program:")), None)
        if section is None:
            continue

        confs[section.split(":", 1)[1].strip()] = {
            "command": parser.get(section, "command", fallback="").strip(),
            "directory": parser.get(section, "directory", fallback="").strip(),
        }

    return confs


# odoo.sh: no systemd/supervisor, one build per host — discovered from the
# env vars its shell sources (PGDATABASE et al.) rather than any
# process-manager listing. Odoo.sh sets these system-wide, not just in an
# interactive login shell's rc file, so a plain `ssh host env` (no `-l`
# needed) sees them too — verified against a live odoo.sh instance.
def _odoosh_env(host: Host = LOCAL) -> dict[str, str] | None:
    """This host's odoo.sh build env, or None off odoo.sh."""
    if host.is_local:
        db = os.environ.get("PGDATABASE")
        version = os.environ.get("ODOO_VERSION", "")
    else:
        out = host.run(["sh", "-c", 'echo "$PGDATABASE"; echo "$ODOO_VERSION"']).stdout
        db, _, version = out.partition("\n")

    if not db:
        return None
    return {"db": db, "version": version.strip()}


def _proc_uptime(pid: str, host: Host = LOCAL) -> float | None:
    """Seconds `pid` has been running, via /proc/<pid>'s own wall-clock
    mtime (process-creation time, NTP-close-enough) against our wall clock —
    None if it's gone.

    Not /proc/<pid>/stat's starttime (clock ticks since boot) diffed against
    /proc/uptime: both are boot-relative, so they're only consistent if they
    share the same boot reference — true on a bare host, but odoo.sh's
    /proc/uptime reflects the shared build node's uptime (weeks/months),
    while a container's own processes can start ticks-since-boot counting
    from a much more recent point, so the subtraction came out as the node's
    uptime, not the worker's. Same divergence class as the systemd
    ActiveEnterTimestamp/CLOCK_BOOTTIME fix noted in host.py's history —
    wall clock instead of a boot-relative clock that isn't guaranteed to be
    shared across a container boundary.
    """
    started = host.stat_mtime(f"/proc/{pid}")
    return None if started is None else time.time() - started


def odoosh_instances(host: Host = LOCAL) -> list[Instance]:
    """The single build `host` is running, when it's odoo.sh.

    One SSH-accessible odoo.sh host is one build, not several — "the
    instance" is just "this box", nothing to enumerate. `uptime` tracks the
    live `odoo-bin` worker rather than the build itself: odoo.sh spawns and
    reaps workers on demand, so "-" here means idle (no worker alive right
    now), not stopped.
    """
    env = _odoosh_env(host)
    if env is None:
        return []

    uptime = "(idle)"
    if (pid := _odoosh_master_pid(host)) is not None and (secs := _proc_uptime(pid, host)) is not None:
        uptime = format_duration(secs)

    return [
        {
            "name": env["db"],
            "status": "running",
            "uptime": uptime,
            "manager": "odoosh",
            "db": env["db"],
            "version": env["version"],
        }
    ]


def _odoosh_master_pid(host: Host = LOCAL) -> str | None:
    """Returns the PID of the top-level `odoo-bin` process, or None.

    PID 1 is the container init and reaps unrelated orphans, so we cannot
    simply walk descendants from it. Instead, we identify the master
    `odoo-bin` as the only "odoo-bin" process whose parent is not also
    an "odoo-bin" process.
    """
    out = host.run(["ps", "-eo", "pid,ppid,args"]).stdout
    odoo_ppid: dict[str, str] = {}

    for line in out.splitlines()[1:]:
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = parts
        if "odoo-bin" in cmd:
            odoo_ppid[pid] = ppid

    return next((pid for pid, ppid in odoo_ppid.items() if ppid not in odoo_ppid), None)


def instance_action(unit: str, action: str, manager: str = "systemd", host: Host = LOCAL) -> str:
    """start/stop/restart an instance via its process manager.

    Odoo instances run under systemd --user, supervisor or odoo.sh; the
    caller passes the `manager` recorded at discovery time so the right
    controller is used. Returns "" on success, else the controller's error
    output (so the UI can show why nothing happened instead of failing
    silently).
    """
    if manager == "odoosh":
        # odoo.sh has no separate start/stop — sleep/wake is the platform's
        # call, not ours; only a restart of the http workers is exposed.
        if action != "restart":
            return "start/stop not supported — odoo.sh handles sleep/wake on its own"

        # odoosh-restart takes one service at a time, unlike `supervisorctl
        # restart` which restarts everything for the instance in one call —
        # so restart both services it's equivalent to.
        for service in ("http", "cron"):
            try:
                out = host.run(["odoosh-restart", service])
            except FileNotFoundError:
                return "odoosh-restart not found on PATH"

            if out.returncode != 0:
                return out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"
        return ""

    # synchronous; --user odoo units activate fast. Move to a worker
    # if a unit's start/restart ever blocks the UI.
    cmd = ["supervisorctl", action, unit] if manager == "supervisor" else ["systemctl", "--user", action, unit]

    try:
        out = host.run(cmd)
    except FileNotFoundError:
        return f"{cmd[0]} not found on PATH"

    if out.returncode == 0:
        return ""

    return out.stderr.strip() or out.stdout.strip() or f"exit {out.returncode}"


# db ownership: a database belongs to an instance when its owner role matches
# the instance. ODOO_ACTIVITY_DB_ROLE forces a single role (locally every db is
# owned by `openerp`); unset, the role is the instance name.
DB_ROLE = os.environ.get("ODOO_ACTIVITY_DB_ROLE", "")

_DB_BY_ROLE_SQL = (
    "SELECT d.datname FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
    "WHERE r.rolname = :'role' AND NOT d.datistemplate ORDER BY 1"
)


def _systemd_workdir(unit: str, host: Host = LOCAL) -> Path:
    """WorkingDirectory of a systemd --user unit (cwd if unset — ours
    locally; a neutral `/` remotely, where our own cwd means nothing)."""
    show = host.run(["systemctl", "--user", "show", unit, "-p", "WorkingDirectory"]).stdout
    if m := re.search(r"WorkingDirectory=(\S+)", show):
        return Path(m.group(1))
    return Path.cwd() if host.is_local else Path("/")


def instance_workdir(inst: Instance, host: Host = LOCAL) -> Path:
    """The instance's working directory (supervisor `directory=`, the
    systemd unit's WorkingDirectory, or $HOME on odoo.sh)."""
    if inst["manager"] == "supervisor":
        return Path(inst.get("directory") or ".")

    if inst["manager"] == "odoosh":
        if host.is_local:
            return Path.home()
        return Path(host.run(["sh", "-c", "echo $HOME"]).stdout.strip() or "/root")

    return _systemd_workdir(inst["name"], host)


def _config_names(instance_name: str) -> list[str]:
    """Config filenames to try, in order.

    A multi-node instance's name is suffixed `-NN` (e.g. `foo-01`), and its
    config is `odooNN.conf` rather than `odoo.conf` — `server.conf` is never
    node-numbered, so it's only ever tried plain.
    """
    if m := re.search(r"-(\d+)$", instance_name):
        return [f"odoo{m.group(1)}.conf", "odoo.conf", "server.conf"]
    return ["odoo.conf", "server.conf"]


def _config_file(inst: Instance, host: Host = LOCAL) -> Path | None:
    """The first `<workdir>/config/` file matching `_config_names`, or the
    fixed `~/.config/odoo/odoo.conf` odoo.sh always writes."""
    if inst["manager"] == "odoosh":
        path = instance_workdir(inst, host) / ".config" / "odoo" / "odoo.conf"
        return path if host.is_file(path) else None

    workdir = instance_workdir(inst, host)
    for name in _config_names(inst["name"]):
        path = workdir / "config" / name
        if host.is_file(path):
            return path

    return None


def configfile_of(inst: Instance, host: Host = LOCAL) -> Path | None:
    """The instance's resolved config file path, for tools (e.g. the
    `odoo-config` CLI) that operate on the file directly rather than its
    parsed values."""
    return _config_file(inst, host)


def instance_config(inst: Instance, host: Host = LOCAL) -> tuple[Path, configparser.RawConfigParser | None]:
    """(workdir, parsed odoo config) — the single source of db + log settings.

    The config is the first of `<workdir>/config/` matching `_config_names`;
    returns (workdir, None) when none exists.
    """
    workdir = instance_workdir(inst, host)
    path = _config_file(inst, host)
    if path is None:
        return workdir, None

    parser = configparser.RawConfigParser()  # odoo configs may contain `%`
    parser.read_string(host.read_text(path))
    return workdir, parser


def _opt(parser: configparser.RawConfigParser | None, key: str) -> str | None:
    """An [options] value, or None for missing / the odoo 'False'."""
    if parser is None:
        return None
    value = parser.get("options", key, fallback="").strip()
    return value if value and value.lower() != "false" else None


def logfile_of(inst: Instance, host: Host = LOCAL) -> Path | None:
    """The instance's odoo logfile, from the `logfile` key of its config, or
    odoo.sh's fixed `~/logs/odoo.log` (its config is sparse — no `logfile`
    key at all)."""
    if inst["manager"] == "odoosh":
        path = instance_workdir(inst, host) / "logs" / "odoo.log"
        return path if host.is_file(path) else None

    workdir, parser = instance_config(inst, host)
    logfile = _opt(parser, "logfile")
    if logfile is None:
        return None

    path = Path(logfile)
    return path if path.is_absolute() else workdir / path


def db_port_of(inst: Instance, host: Host = LOCAL) -> str | None:
    """The instance's postgres port from its odoo config, or None for the
    cluster default (instances may run on different clusters)."""
    _, parser = instance_config(inst, host)
    return _opt(parser, "db_port")


def databases_of(inst: Instance, host: Host = LOCAL) -> tuple[list[str], str | None]:
    """(databases, db_port) for the instance — its authoritative members and
    the postgres port they live on (instances may run on different clusters).

    The odoo config gives both the role (db_user — locally `openerp`, in prod
    the instance's own role) and the db_port, so we query the right role on the
    right postgres cluster.

    odoo.sh is a single env-provided db (`PGDATABASE`), not role-queried —
    there's no `databases_by_role` dance since there's exactly one db.

    Reads `db_port` off the same parser as `db_user` rather than calling
    `db_port_of` (which would re-fetch and re-parse the config from
    scratch) — cheap to duplicate locally, but each fetch is its own ssh
    round trip remotely.
    """
    if inst["manager"] == "odoosh":
        return [inst["db"]], None

    _, parser = instance_config(inst, host)
    port = _opt(parser, "db_port")
    role = DB_ROLE or _opt(parser, "db_user") or inst["name"].removesuffix(".service")
    return databases_by_role(role, port, host), port


def databases_by_role(role: str, port: str | None = None, host: Host = LOCAL) -> list[str]:
    """Non-template databases owned by `role`, via psql on `port`. Empty if
    postgres is unreachable (so the UI degrades instead of crashing)."""
    # SQL comes in on stdin (-f -) so psql expands :'role' and quotes it safely;
    # -c does no variable interpolation.
    cmd = ["psql", "-d", "postgres"]
    if port:
        cmd += ["-p", port]

    cmd += ["-v", f"role={role}", "-tA", "-f", "-"]
    out = host.run(cmd, input_text=_DB_BY_ROLE_SQL).stdout

    return [line.strip() for line in out.splitlines() if line.strip()]


_LONG_QUERIES_SQL = (
    "SELECT json_agg(t) FROM ("
    "SELECT pid, datname, query_start, age(now(), query_start) AS duration, query "
    "FROM pg_stat_activity "
    "WHERE state != 'idle' AND pid != pg_backend_pid() AND datname = :'db' "
    "ORDER BY duration DESC"
    ") t"
)


def long_queries(db: str, port: str | None = None, host: Host = LOCAL) -> list[dict]:
    """Non-idle queries on `db`, longest-running first, via psql on `port`.

    odoo-db has no equivalent command; this queries pg_stat_activity
    directly instead, the same way databases_by_role reads pg_database.
    """
    cmd = ["psql", "-d", "postgres"]
    if port:
        cmd += ["-p", port]

    cmd += ["-v", f"db={db}", "-tA", "-f", "-"]
    out = host.run(cmd, input_text=_LONG_QUERIES_SQL).stdout

    try:
        return json.loads(out.strip()) or []
    except (json.JSONDecodeError, ValueError):
        return []


def _parse_cpu_ticks(data: str) -> int | None:
    try:
        fields = data[data.rindex(")") + 2 :].split()  # skip 'pid (comm)'
        return int(fields[11]) + int(fields[12])  # utime + stime
    except (ValueError, IndexError):
        return None


def proc_cpu_ticks(pid: str, host: Host = LOCAL) -> int | None:
    """utime+stime (CPU jiffies) for a pid, or None if it's gone."""
    try:
        data = host.read_text(f"/proc/{pid}/stat")
    except OSError:
        return None
    return _parse_cpu_ticks(data)


def proc_cpu_ticks_many(pids: list[str], host: Host = LOCAL) -> dict[str, int | None]:
    """Batched `proc_cpu_ticks` -- local stays one read per pid (microseconds
    each, no need to batch), but remote becomes one ssh round trip for every
    pid instead of one round trip per pid: the Processes tab was sitting on
    "Loading processes..." for several real seconds against a host with more
    than a couple of processes, one sequential round trip at a time."""
    if host.is_local or not pids:
        return {pid: proc_cpu_ticks(pid, host) for pid in pids}

    marker = "\x1e"  # ASCII record separator -- won't appear in /proc/*/stat
    script = ";".join(f"echo {marker}{pid}; cat /proc/{pid}/stat 2>/dev/null" for pid in pids)
    out = host.run(["sh", "-c", script]).stdout

    result: dict[str, int | None] = dict.fromkeys(pids)
    for block in out.split(marker)[1:]:
        pid, _, data = block.partition("\n")
        result[pid.strip()] = _parse_cpu_ticks(data)
    return result


def instance_pid(inst: Instance, host: Host = LOCAL) -> str | None:
    """The instance's master pid, straight from its process manager.

    Not matched by database name: in multi-db/config-only setups the odoo
    process's argv never carries a db name at all (only postgres's own
    backends do, since they connect to a specific db), so a db-name-in-argv
    heuristic both misses the real process and can misfire on postgres.
    """
    if inst["manager"] == "odoosh":
        return _odoosh_master_pid(host)

    if inst["manager"] == "supervisor":
        out = host.run(["supervisorctl", "pid", inst["name"]]).stdout.strip()
        return out if out.isdigit() else None

    out = host.run(["systemctl", "--user", "show", inst["name"], "-p", "MainPID"]).stdout
    m = re.search(r"MainPID=(\d+)", out)
    return m.group(1) if m and m.group(1) != "0" else None


def procs_of(inst: Instance, host: Host = LOCAL) -> list[ProcRow]:
    """The instance's master process plus every descendant (prefork
    workers), read purely from ps by walking the ppid tree down from the
    manager-reported master pid."""
    master = instance_pid(inst, host)
    if master is None:
        return []

    lines = host.run(["ps", "-eo", "pid,ppid,user,%mem,args"]).stdout.splitlines()[1:]  # drop header

    by_pid: dict[str, ProcRow] = {}
    children: dict[str, list[str]] = {}
    for ln in lines:
        cols = ln.split(maxsplit=4)
        if len(cols) < 5:
            continue

        row: ProcRow = {"pid": cols[0], "ppid": cols[1], "user": cols[2], "mem": cols[3], "cmd": cols[4]}
        by_pid[row["pid"]] = row
        children.setdefault(row["ppid"], []).append(row["pid"])

    keep: list[str] = []
    stack = [master]
    while stack:
        pid = stack.pop()
        if pid in keep or pid not in by_pid:
            continue
        keep.append(pid)
        stack.extend(children.get(pid, []))

    return [by_pid[pid] for pid in keep]


def instance_procs(inst: Instance, host: Host = LOCAL) -> tuple[list[ProcRow], list[ProcRow]]:
    """(odoo_processes, postgres_backends) from one `ps` call, halving the
    system-wide `ps` fork+parse the Processes tab used to do twice a tick.

    Postgres process titles vary by cluster configuration, so backends are
    matched by db-name membership rather than a fixed token position.
    """
    master = instance_pid(inst, host)
    dbs = set(databases_of(inst, host)[0])

    lines = host.run(["ps", "-eo", "pid,ppid,user,%mem,args"]).stdout.splitlines()[1:]

    by_pid: dict[str, ProcRow] = {}
    children: dict[str, list[str]] = {}
    pg_rows: list[ProcRow] = []

    for ln in lines:
        cols = ln.split(maxsplit=4)
        if len(cols) < 5:
            continue

        row: ProcRow = {"pid": cols[0], "ppid": cols[1], "user": cols[2], "mem": cols[3], "cmd": cols[4]}
        by_pid[row["pid"]] = row
        children.setdefault(row["ppid"], []).append(row["pid"])

        if dbs and row["cmd"].startswith("postgres:") and dbs.intersection(row["cmd"].split()):
            pg_rows.append(row)

    odoo_rows: list[ProcRow] = []
    if master is not None:
        keep: list[str] = []
        stack = [master]
        while stack:
            pid = stack.pop()
            if pid in keep or pid not in by_pid:
                continue
            keep.append(pid)
            stack.extend(children.get(pid, []))
        odoo_rows = [by_pid[pid] for pid in keep]

    return odoo_rows, pg_rows


_PG_CLIENT_PORT_RE = re.compile(r"\((\d+)\)")


def pg_client_port(cmd: str) -> str | None:
    """The client TCP port out of a postgres backend's `ps` title, or None
    over a unix socket (`[local]`, no parenthesized port at all)."""
    m = _PG_CLIENT_PORT_RE.search(cmd)
    return m.group(1) if m else None


def odoo_pid_for_port(port: str, host: Host = LOCAL) -> str | None:
    """The pid on the other end of the TCP connection whose port is `port`
    (a postgres backend's client port), via `lsof` — traces a postgres
    backend back to the Odoo worker that opened it. `lsof -i :port` matches
    the connection from either endpoint, so postgres's own accepting side
    is excluded by name; None if `lsof` is missing or nothing else matches."""
    try:
        out = host.run(["lsof", "-Pni", f":{port}"]).stdout
    except FileNotFoundError:
        return None

    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) > 1 and cols[0] != "postgres" and cols[1].isdigit():
            return cols[1]

    return None


def signal_process(pid: str, sig: int, host: Host = LOCAL) -> None:
    """Send `sig` to `pid`; a pid that's already gone, or owned by another
    user (e.g. a postgres backend), is not an error."""
    if host.is_local:
        with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
            os.kill(int(pid), sig)
        return

    host.run(["kill", f"-{int(sig)}", pid])


# Matches Odoo's standard log header for SIGQUIT dumps to extract worker PIDs:
# "<date> <time> <pid> <level> <db> odoo.tools.misc: "
_DUMP_HEADER_RE = re.compile(r"^\S+ \S+ (?P<pid>\d+) \S+ \S+ odoo\.tools\.misc:[ \t]*$", re.MULTILINE)
# Odoo's dumpstacks() (odoo/tools/misc.py) emits this shape for every thread,
# request or not — db/uid/url/qc/qt/pt are "n/a" when the thread isn't
# mid-request. qt = query_time (SQL time so far); pt = python_time, Odoo's
# own name for wall-clock-since-request-start minus qt (time spent outside
# the DB) — not a running total SIGQUIT accumulates, just what's true at
# this one signal. db/uid/url predate qc/qt/pt, which only exist from Odoo
# 17+; that trailing group is optional so pre-17 dumps still parse, just
# without query-count/time context.
_THREAD_RE = re.compile(
    r"^# Thread: (?P<name>.*?) \(db:(?P<db>[^)]*)\) \(uid:(?P<uid>[^)]*)\) \(url:(?P<url>[^)]*)\)"
    r"(?: \(qc:(?P<qc>\S*) qt:(?P<qt>\S*) pt:(?P<pt>\S*)\))?$",
    re.MULTILINE,
)
_FRAME_RE = re.compile(r'^File: "(?P<file>[^"]*)", line (?P<line>\d+), in (?P<func>\S+)$', re.MULTILINE)

# Innermost frame functions that indicate an idle thread (event loops, waits,
# and faulthandler frames).
_IDLE_FRAME_FUNCS = {"select", "poll", "sleep", "wait", "dumpstacks", "extract_stack"}

# Vendor-specific path override for Sentry's background thread, which is
# always present but constantly idle.
_IDLE_FRAME_PATH_MARKERS = ("/sentry_sdk/",)


class _InstanceRequired(TypedDict):
    name: str
    status: str
    uptime: str
    manager: str


class Instance(_InstanceRequired, total=False):
    """`command`/`directory` come from supervisor, `db`/`version` from
    odoo.sh — each manager only ever sets its own subset."""

    command: str
    directory: str
    db: str
    version: str


class ProcRow(TypedDict):
    pid: str
    ppid: str
    user: str
    mem: str
    cmd: str


class Worker(TypedDict):
    pid: str
    threads: list[Thread]


class Thread(TypedDict):
    name: str
    db: str | None
    uid: str | None
    url: str | None
    query_count: int | None
    query_time: float | None
    python_time: float | None
    frames: list[Frame]
    idle: bool


class Frame(TypedDict):
    file: str
    line: int
    func: str


def parse_stack_dump(text: str) -> list[Worker]:
    """A slice of log text containing one or more SIGQUIT dumps into
    `[{"pid": ..., "threads": [Thread, ...]}, ...]`.

    Each `Thread` carries its request context (`db`/`uid`/`url`/`query_count`/
    `query_time`/`python_time`) when Odoo attached one — see `Thread` and the
    `_THREAD_RE` comment for what `query_time`/`python_time` mean. `frames` is
    outermost-first (as printed); a thread is `idle` iff its *innermost*
    (last) frame's function is in `_IDLE_FRAME_FUNCS`.
    """
    markers = list(_DUMP_HEADER_RE.finditer(text))
    workers: list[Worker] = []

    for i, dm in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        workers.append({"pid": dm["pid"], "threads": _parse_threads(text[dm.end() : end])})

    return workers


def _parse_threads(block_text: str) -> list[Thread]:
    headers = list(_THREAD_RE.finditer(block_text))
    threads: list[Thread] = []

    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(block_text)
        frames: list[Frame] = [
            {"file": fm["file"], "line": int(fm["line"]), "func": fm["func"]}
            for fm in _FRAME_RE.finditer(block_text[m.end() : end])
        ]
        idle = bool(frames) and (
            frames[-1]["func"] in _IDLE_FRAME_FUNCS
            or any(marker in frames[-1]["file"] for marker in _IDLE_FRAME_PATH_MARKERS)
        )
        threads.append({
            "name": m["name"].strip(),
            "db": _na(m["db"]),
            "uid": _na(m["uid"]),
            "url": _na(m["url"]),
            "query_count": _opt_int(m["qc"]),
            "query_time": _opt_float(m["qt"]),
            "python_time": _opt_float(m["pt"]),
            "frames": frames,
            "idle": idle,
        })

    return threads


def _na(value: str | None) -> str | None:
    """Odoo prints "n/a" for a thread attribute that isn't set (not
    mid-request); normalize that to None. `value` is also None outright on
    pre-17 dumps, where `_THREAD_RE`'s qc/qt/pt group didn't match at all."""
    return None if value in (None, "n/a") else value


def _opt_int(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def _opt_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _read_new_bytes(proc: subprocess.Popen) -> bytes:
    """Whatever's already buffered on a streaming child's stdout, without
    blocking — for polling a `tail -f` child instead of a blocking read."""
    if proc.stdout is None or not select.select([proc.stdout], [], [], 0)[0]:
        return b""
    return os.read(proc.stdout.fileno(), 65536)


def stacks_by_activity(workers: list[Worker]) -> list[Worker]:
    """`workers` reordered busy-first for display: workers by busy-thread
    count descending, threads within each busy-before-idle, and each
    thread's `frames` reversed to innermost-first -- py-spy's order, so the
    concerning frame is the one a reader (human or agent) sees first rather
    than last."""
    by_busy = sorted(workers, key=lambda w: sum(not t["idle"] for t in w["threads"]), reverse=True)
    return [
        {
            **w,
            "threads": [
                {**t, "frames": list(reversed(t["frames"]))} for t in sorted(w["threads"], key=lambda t: t["idle"])
            ],
        }
        for w in by_busy
    ]


def dump_and_parse_stacks(inst: Instance, host: Host = LOCAL) -> tuple[str, list[Worker]]:
    """SIGQUIT all instance processes, read new log output, and parse stack dumps.

    Sends SIGQUIT to master and descendant workers, reading the log file from the
    pre-signal offset. Polls up to ~2s for all expected PID headers to appear.

    Returns (error, workers); error is non-empty (workers `[]`) only when there's
    truly nothing to show (no workers, no logfile, or nothing arrived at
    all)."""
    path = logfile_of(inst, host)
    if path is None or not host.is_file(path):
        return "(no logfile configured)", []

    procs = procs_of(inst, host)
    if not procs:
        return "(no workers alive)", []

    before = host.stat_size(path) or 0
    for proc in procs:
        signal_process(proc["pid"], signal.SIGQUIT, host)

    expected = {proc["pid"] for proc in procs}
    text = ""

    if host.is_local:
        for _ in range(20):  # ~2s budget
            # seek to the pre-signal offset instead of rereading the whole
            # file each poll — on a multi-GB log that reread alone made the
            # dump take ages regardless of how small the actual new output was.
            with path.open("rb") as f:
                f.seek(before)
                text = f.read().decode(errors="replace")
            seen = {m["pid"] for m in _DUMP_HEADER_RE.finditer(text)}
            if expected <= seen:
                break
            time.sleep(0.1)
    else:
        # one round trip to start a streaming reader from the pre-signal
        # offset, instead of up to 20 separate ssh round trips for the poll
        # loop above -- see the local branch's own comment on why re-reading
        # from the start would be bad on a multi-GB log, remote or not.
        proc = host.popen(["tail", "-f", "-c", f"+{before + 1}", str(path)], stderr=subprocess.DEVNULL, text=False)
        try:
            data = b""
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                data += _read_new_bytes(proc)
                text = data.decode(errors="replace")
                seen = {m["pid"] for m in _DUMP_HEADER_RE.finditer(text)}
                if expected <= seen:
                    break
                time.sleep(0.1)
        finally:
            proc.kill()

    if not text.strip():
        return "(dump did not appear in the log)", []

    return "", parse_stack_dump(text)


def instance_version(inst: Instance, host: Host = LOCAL) -> str | None:
    """The instance's Odoo version, via the `odoo-addons-path` CLI (layout/
    addons-path detection lives there, not here) — or straight from
    odoo.sh's own `$ODOO_VERSION` env var, captured at discovery time."""
    if inst["manager"] == "odoosh":
        return inst.get("version")

    try:
        out = host.run(["odoo-addons-path", str(instance_workdir(inst, host)), "--verbose", "--format", "json"]).stdout
    except FileNotFoundError:
        return None

    try:
        return json.loads(out).get("version")
    except (json.JSONDecodeError, ValueError):
        return None


def render_config(config: Path, version: str | None, mode: str, host: Host = LOCAL) -> str:
    """`odoo-config <mode> <config>` output — plain ini text (compact = only
    keys differing from odoo's default; expand = every valid option filled
    in). `version` is omitted when unknown; odoo-config then falls back to
    its newest schema."""
    cmd = ["odoo-config", mode, str(config)]
    if version:
        cmd += ["--version", version]

    try:
        out = host.run(cmd)
    except FileNotFoundError:
        return "(odoo-config not found on PATH)"

    return out.stdout.strip() or out.stderr.strip() or f"(odoo-config exit {out.returncode})"


_TAIL_CHUNK = 64 * 1024


def tail(path: Path, lines: int = 200, host: Host = LOCAL) -> str:
    """Last `lines` of a file, or a short note if it can't be read.

    Local: reads backward in chunks from the end instead of scanning the
    whole file — a multi-GB logfile shouldn't cost more than a few reads.
    Remote: one `tail -n` call does the same job server-side.
    """
    if not host.is_local:
        result = host.run(["tail", "-n", str(lines), str(path)])
        return result.stdout if result.returncode == 0 else f"(no log: {result.stderr.strip()})"

    try:
        with path.open("rb") as f:
            end = f.seek(0, 2)
            pos = end
            data = b""

            while pos > 0 and data.count(b"\n") <= lines:
                pos = max(0, pos - _TAIL_CHUNK)
                f.seek(pos)
                data = f.read(end - pos)

            text = data.decode(errors="replace")
            return "\n".join(text.splitlines()[-lines:])
    except OSError as exc:
        return f"(no log: {exc})"


def start_odoo_db(command: str, db: str, port: str | None = None, host: Host = LOCAL) -> subprocess.Popen[str] | None:
    """Start `odoo-db --output-format json <command> <db>`, on `port` if the
    instance's cluster isn't the default one (odoo-db has no --port flag of
    its own, but honors PGPORT like any libpq client — set via an `env`
    prefix rather than subprocess's `env=` kwarg, so it works the same
    whether `odoo-db` runs locally or over ssh).

    Returns the live process rather than waiting on it, so a caller can
    `.kill()` it if abandoned (e.g. the tab driving it was switched away
    from) instead of blocking behind a slow query. Note this only stops *our*
    client and its thread — odoo-db opens a plain psycopg connection with no
    SIGTERM handling, so Postgres notices the dropped connection and cancels
    the backend query on its own schedule, not instantly
    (see odoo-db/db.py connect()).

    None if `odoo-db` isn't on PATH (degrade like render_config does for
    odoo-config, instead of crashing the app on a host that lacks it).
    """
    cmd = ["env", f"PGPORT={port}"] if port else []
    cmd += ["odoo-db", "--output-format", "json", command]
    if command == "crons":
        # show scheduled actions' code
        cmd += ["--include-code"]
    cmd += [db]

    try:
        return host.popen(cmd)
    except FileNotFoundError:
        return None


def parse_odoo_db_output(stdout: str, stderr: str) -> tuple[list[dict] | None, str]:
    """(rows, raw) from a `start_odoo_db` process's captured output.

    `rows` is None when the output isn't JSON (e.g. a plain message like
    "queue_job module not installed."); `raw` is then the message to show
    as-is.
    """
    raw = stdout.strip() or stderr.strip()

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, raw

    if isinstance(data, dict):
        data = [data]

    return data, raw


def table_columns(rows: list[dict]) -> list[str]:
    """Union of keys across rows, preserving first-seen order (columns vary
    per odoo-db command)."""
    columns: list[str] = []

    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    return columns


def stringify(value: object, max_cell: int = 80) -> str:
    """Render a cell: nested values (dict/list, e.g. `attachments`) as compact
    JSON, clipped to `max_cell`."""
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, dict | list) else str(value)

    return text if len(text) <= max_cell else text[: max_cell - 1] + "…"


_LOGO = r"""
   /ss/                              :so
   /hh/                              -hho
 --/hho--.     .-----.    .----.     -hho  .-----.
 +hhhhhhh+   ./yhhhhhhs+oyhhhhhhyo-  -hhs+shhhhhhhy++ossssssssss+'
   /hh/     /yhy+-' '/yhho/-...:shhs.-hhhhy/-...-/yhyo:----:sss/
   :hh/    /hhs'    -hhy.        -yhy/hhh+'       '/hhs   'oss:
   :hh/    shy.     shh-          -hhyhhs           shh- .oss-
   :hh/   'hhy      shh.          -hhyhhs           ohh:-sso.
   :hh/   'hhy      :hho'        .shy-shh/         /hhy/ss+'
   -hhs'  'hhy       /yhy+-'  '-+yhy: 'shhs/.' '.:shhsoss+'
    +yhhyhohhy        '/shhhyhhhys/'    -oyhhhyhhhyo:ossssssssssss+'
     '-///:///          '.-://:-'          .:///:.' -:::::::::::::::
"""


def about_text() -> str:
    """Static overview shown when the host pane is focused."""
    return (
        f"{_LOGO}\nhost: {socket.gethostname()}\n{platform.platform()}\n\nodoo-activity — local Odoo instance monitor"
    )
