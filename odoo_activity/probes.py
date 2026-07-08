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
import socket
import subprocess
import time
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK")


def read_uptime() -> float:
    """System uptime in seconds, from /proc/uptime."""
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def read_loadavg() -> tuple[float, float, float]:
    """1/5/15-minute load averages, from /proc/loadavg."""
    one, five, fifteen = Path("/proc/loadavg").read_text().split()[:3]
    return float(one), float(five), float(fifteen)


def format_duration(seconds: float) -> str:
    """`H:MM:SS`, or `<D>d HH:MM:SS` past a day."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{hours}:{minutes:02d}:{secs:02d}"


def read_cpu_times() -> tuple[int, int]:
    """Return (total, idle) jiffies from /proc/stat."""
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]

    idle = vals[3] + vals[4]  # idle + iowait

    return sum(vals), idle


def read_mem() -> tuple[float, float]:
    """Return (mem_used_pct, swap_used_pct) from /proc/meminfo."""
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, rest = line.split(":", 1)
            info[key] = int(rest.split()[0])  # kB

    mem_pct = (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"] * 100
    swap_total = info["SwapTotal"]
    swap_pct = (swap_total - info["SwapFree"]) / swap_total * 100 if swap_total else 0.0

    return mem_pct, swap_pct


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


def list_instances() -> list[dict[str, str]]:
    """All local Odoo instances, from systemd --user and supervisor.

    Each row carries its `manager` so actions route to the right controller;
    the two managers can even expose the same name (e.g. odoo-demo).
    """
    return systemd_instances() + supervisor_instances()


def systemd_instances() -> list[dict[str, str]]:
    """Odoo instances from systemd --user units.

    Uses list-unit-files (catches stopped units, which list-units hides) then
    one batched `show` to read Description + state for each. Matches the unit
    name or Description so name-convention units (openerp-*.service) are caught.
    """
    # --user only; add system-wide (`systemctl` without --user) when a
    # host needs it.
    files = subprocess.run(
        ["systemctl", "--user", "list-unit-files", "--type=service", "--no-legend", "--plain", "--no-pager"],
        capture_output=True,
        text=True,
    ).stdout

    # drop template units (foo@.service) — `show` errors out on them
    units = [tok for tok in files.split() if tok.endswith(".service") and not tok.endswith("@.service")]
    if not units:
        return []

    out = subprocess.run(
        [
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
        ],
        capture_output=True,
        text=True,
    ).stdout
    instances = []
    # systemd's *TimestampMonotonic properties are CLOCK_MONOTONIC (excludes
    # suspended time) — diffing against CLOCK_BOOTTIME (includes it) would
    # overstate uptime by the machine's total suspend time since boot
    now = time.clock_gettime(time.CLOCK_MONOTONIC)

    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = props.get("Id", "")
        if not _is_odoo(name, props.get("Description", "")):
            continue

        status = _SYSTEMD_STATUS.get(props.get("ActiveState", ""), "stopped")
        uptime = "-"
        if status == "running" and (entered := int(props.get("ActiveEnterTimestampMonotonic", "0") or 0)):
            uptime = format_duration(now - entered / 1_000_000)

        instances.append({"name": name, "status": status, "uptime": uptime, "manager": "systemd"})

    return instances


# supervisor programs are declared one-per-file here on servers; the
# [program:x] section carries `directory=` (the instance's odoo dir).
# Below is the standard path on the server.
SUPERVISOR_CONFD = Path("/opt/openerp/supervisor/conf.d")


def supervisor_instances() -> list[dict[str, str]]:
    """Odoo instances under supervisor.

    Names + `directory`/`command` come from the conf.d programs; running state
    comes from `supervisorctl status`. Works with either source alone — a host
    without the conf.d layout still lists what supervisorctl reports.
    """
    states = _supervisor_states()
    confs = _supervisor_confs()
    instances = []

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


def _supervisor_states() -> dict[str, dict[str, str]]:
    """program -> {status, uptime} from `supervisorctl status` (skips the
    pkg_resources banner and any non-status lines). `uptime` is supervisor's
    own `H:MM:SS`/`D:HH:MM:SS` text, lifted straight out of the status line —
    only RUNNING/STARTING programs carry one. Returns {} if supervisor isn't
    installed on this host."""
    try:
        out = subprocess.run(
            ["supervisorctl", "status"],
            capture_output=True,
            text=True,
        ).stdout
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


def _supervisor_confs() -> dict[str, dict[str, str]]:
    """program -> {command, directory} parsed from SUPERVISOR_CONFD/*.conf."""
    if not SUPERVISOR_CONFD.is_dir():
        return {}

    confs = {}

    for path in sorted(SUPERVISOR_CONFD.glob("*.conf")):
        parser = configparser.RawConfigParser(strict=False)  # supervisor uses %
        try:
            parser.read(path)
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


def instance_action(unit: str, action: str, manager: str = "systemd") -> str:
    """start/stop/restart an instance via its process manager.

    Odoo instances run under systemd --user or supervisor; the caller passes
    the `manager` recorded at discovery time so the right controller is used.
    Returns "" on success, else the controller's error output (so the UI can
    show why nothing happened instead of failing silently).
    """
    # synchronous; --user odoo units activate fast. Move to a worker
    # if a unit's start/restart ever blocks the UI.
    cmd = ["supervisorctl", action, unit] if manager == "supervisor" else ["systemctl", "--user", action, unit]

    out = subprocess.run(cmd, capture_output=True, text=True)

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


def _systemd_workdir(unit: str) -> Path:
    """WorkingDirectory of a systemd --user unit (cwd if unset)."""
    show = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "WorkingDirectory"],
        capture_output=True,
        text=True,
    ).stdout

    return Path(m.group(1)) if (m := re.search(r"WorkingDirectory=(\S+)", show)) else Path.cwd()


def instance_workdir(inst: dict) -> Path:
    """The instance's working directory (supervisor `directory=` or the
    systemd unit's WorkingDirectory)."""
    if inst["manager"] == "supervisor":
        return Path(inst.get("directory") or ".")

    return _systemd_workdir(inst["name"])


def _config_names(instance_name: str) -> list[str]:
    """Config filenames to try, in order.

    A multi-node instance's name is suffixed `-NN` (e.g. `foo-01`), and its
    config is `odooNN.conf` rather than `odoo.conf` — `server.conf` is never
    node-numbered, so it's only ever tried plain.
    """
    if m := re.search(r"-(\d+)$", instance_name):
        return [f"odoo{m.group(1)}.conf", "odoo.conf", "server.conf"]
    return ["odoo.conf", "server.conf"]


def _config_file(inst: dict) -> Path | None:
    """The first `<workdir>/config/` file matching `_config_names`, or None."""
    workdir = instance_workdir(inst)
    for name in _config_names(inst["name"]):
        path = workdir / "config" / name
        if path.is_file():
            return path

    return None


def configfile_of(inst: dict) -> Path | None:
    """The instance's resolved config file path, for tools (e.g. the
    `odoo-config` CLI) that operate on the file directly rather than its
    parsed values."""
    return _config_file(inst)


def instance_config(inst: dict) -> tuple[Path, configparser.RawConfigParser | None]:
    """(workdir, parsed odoo config) — the single source of db + log settings.

    The config is the first of `<workdir>/config/` matching `_config_names`;
    returns (workdir, None) when none exists.
    """
    workdir = instance_workdir(inst)
    path = _config_file(inst)
    if path is None:
        return workdir, None

    parser = configparser.RawConfigParser()  # odoo configs may contain `%`
    parser.read(path)
    return workdir, parser


def _opt(parser: configparser.RawConfigParser | None, key: str) -> str | None:
    """An [options] value, or None for missing / the odoo 'False'."""
    if parser is None:
        return None
    value = parser.get("options", key, fallback="").strip()
    return value if value and value.lower() != "false" else None


def logfile_of(inst: dict) -> Path | None:
    """The instance's odoo logfile, from the `logfile` key of its config."""
    workdir, parser = instance_config(inst)
    logfile = _opt(parser, "logfile")
    if logfile is None:
        return None

    path = Path(logfile)
    return path if path.is_absolute() else workdir / path


def db_port_of(inst: dict) -> str | None:
    """The instance's postgres port from its odoo config, or None for the
    cluster default (instances may run on different clusters)."""
    _, parser = instance_config(inst)
    return _opt(parser, "db_port")


def databases_of(inst: dict) -> tuple[list[str], str | None]:
    """(databases, db_port) for the instance — its authoritative members and
    the postgres port they live on (instances may run on different clusters).

    The odoo config gives both the role (db_user — locally `openerp`, in prod
    the instance's own role) and the db_port, so we query the right role on the
    right postgres cluster.
    """
    _, parser = instance_config(inst)
    port = db_port_of(inst)
    role = DB_ROLE or _opt(parser, "db_user") or inst["name"].removesuffix(".service")
    return databases_by_role(role, port), port


def databases_by_role(role: str, port: str | None = None) -> list[str]:
    """Non-template databases owned by `role`, via psql on `port`. Empty if
    postgres is unreachable (so the UI degrades instead of crashing)."""
    # SQL comes in on stdin (-f -) so psql expands :'role' and quotes it safely;
    # -c does no variable interpolation.
    cmd = ["psql", "-d", "postgres"]
    if port:
        cmd += ["-p", port]

    cmd += ["-v", f"role={role}", "-tA", "-f", "-"]
    out = subprocess.run(cmd, input=_DB_BY_ROLE_SQL, capture_output=True, text=True)

    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def proc_cpu_ticks(pid: str) -> int | None:
    """utime+stime (CPU jiffies) for a pid, or None if it's gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None

    fields = data[data.rindex(")") + 2 :].split()  # skip 'pid (comm)'
    return int(fields[11]) + int(fields[12])  # utime + stime


def instance_pid(inst: dict) -> str | None:
    """The instance's master pid, straight from its process manager.

    Not matched by database name: in multi-db/config-only setups the odoo
    process's argv never carries a db name at all (only postgres's own
    backends do, since they connect to a specific db), so a db-name-in-argv
    heuristic both misses the real process and can misfire on postgres.
    """
    if inst["manager"] == "supervisor":
        out = subprocess.run(
            ["supervisorctl", "pid", inst["name"]],
            capture_output=True,
            text=True,
        ).stdout.strip()
        return out if out.isdigit() else None

    out = subprocess.run(
        ["systemctl", "--user", "show", inst["name"], "-p", "MainPID"],
        capture_output=True,
        text=True,
    ).stdout
    m = re.search(r"MainPID=(\d+)", out)
    return m.group(1) if m and m.group(1) != "0" else None


def procs_of(inst: dict) -> list[dict[str, str]]:
    """The instance's master process plus every descendant (prefork
    workers), read purely from ps by walking the ppid tree down from the
    manager-reported master pid."""
    master = instance_pid(inst)
    if master is None:
        return []

    lines = subprocess.run(
        ["ps", "-eo", "pid,ppid,user,%mem,args"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()[1:]  # drop header

    by_pid: dict[str, dict[str, str]] = {}
    children: dict[str, list[str]] = {}
    for ln in lines:
        cols = ln.split(maxsplit=4)
        if len(cols) < 5:
            continue

        row = {"pid": cols[0], "ppid": cols[1], "user": cols[2], "mem": cols[3], "cmd": cols[4]}
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


def instance_procs(inst: dict) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(odoo_processes, postgres_backends) from one `ps` call, halving the
    system-wide `ps` fork+parse the Processes tab used to do twice a tick.

    Postgres process titles vary by cluster configuration, so backends are
    matched by db-name membership rather than a fixed token position.
    """
    master = instance_pid(inst)
    dbs = set(databases_of(inst)[0])

    lines = subprocess.run(
        ["ps", "-eo", "pid,ppid,user,%mem,args"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()[1:]

    by_pid: dict[str, dict[str, str]] = {}
    children: dict[str, list[str]] = {}
    pg_rows: list[dict[str, str]] = []

    for ln in lines:
        cols = ln.split(maxsplit=4)
        if len(cols) < 5:
            continue

        row = {"pid": cols[0], "ppid": cols[1], "user": cols[2], "mem": cols[3], "cmd": cols[4]}
        by_pid[row["pid"]] = row
        children.setdefault(row["ppid"], []).append(row["pid"])

        if dbs and row["cmd"].startswith("postgres:") and dbs.intersection(row["cmd"].split()):
            pg_rows.append(row)

    odoo_rows: list[dict[str, str]] = []
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


def odoo_pid_for_port(port: str) -> str | None:
    """The pid on the other end of the TCP connection whose port is `port`
    (a postgres backend's client port), via `lsof` — traces a postgres
    backend back to the Odoo worker that opened it. `lsof -i :port` matches
    the connection from either endpoint, so postgres's own accepting side
    is excluded by name; None if `lsof` is missing or nothing else matches."""
    try:
        out = subprocess.run(
            ["lsof", "-Pni", f":{port}"],
            capture_output=True,
            text=True,
        ).stdout
    except FileNotFoundError:
        return None

    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) > 1 and cols[0] != "postgres" and cols[1].isdigit():
            return cols[1]

    return None


def signal_process(pid: str, sig: int) -> None:
    """Send `sig` to `pid`; a pid that's already gone is not an error."""
    with contextlib.suppress(ProcessLookupError, ValueError):
        os.kill(int(pid), sig)


def instance_version(inst: dict) -> str | None:
    """The instance's Odoo version, via the `odoo-addons-path` CLI (layout/
    addons-path detection lives there, not here)."""
    try:
        out = subprocess.run(
            ["odoo-addons-path", str(instance_workdir(inst)), "--verbose", "--format", "json"],
            capture_output=True,
            text=True,
        ).stdout
    except FileNotFoundError:
        return None

    try:
        return json.loads(out).get("version")
    except (json.JSONDecodeError, ValueError):
        return None


def render_config(config: Path, version: str | None, mode: str) -> str:
    """`odoo-config <mode> <config>` output — plain ini text (compact = only
    keys differing from odoo's default; expand = every valid option filled
    in). `version` is omitted when unknown; odoo-config then falls back to
    its newest schema."""
    cmd = ["odoo-config", mode, str(config)]
    if version:
        cmd += ["--version", version]

    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return "(odoo-config not found on PATH)"

    return out.stdout.strip() or out.stderr.strip() or f"(odoo-config exit {out.returncode})"


_TAIL_CHUNK = 64 * 1024


def tail(path: Path, lines: int = 200) -> str:
    """Last `lines` of a file, or a short note if it can't be read.

    Reads backward in chunks from the end instead of scanning the whole
    file — a multi-GB logfile shouldn't cost more than a few reads.
    """
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


def start_odoo_db(command: str, db: str, port: str | None = None) -> subprocess.Popen[str]:
    """Start `odoo-db --output-format json <command> <db>`, on `port` if the
    instance's cluster isn't the default one (odoo-db has no --port flag of
    its own, but honors PGPORT like any libpq client).

    Returns the live process rather than waiting on it, so a caller can
    `.kill()` it if abandoned (e.g. the tab driving it was switched away
    from) instead of blocking behind a slow query. Note this only stops *our*
    client and its thread — odoo-db opens a plain psycopg connection with no
    SIGTERM handling, so Postgres notices the dropped connection and cancels
    the backend query on its own schedule, not instantly
    (see odoo-db/db.py connect()).
    """
    env = {**os.environ, "PGPORT": port} if port else None
    return subprocess.Popen(
        ["odoo-db", "--output-format", "json", command, db],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


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
