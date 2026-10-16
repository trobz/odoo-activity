"""oa-mcp — MCP server exposing odoo-activity's read-only capacities.

POC. Every tool is a thin wrapper over :mod:`odoo_activity.probes` — no
logic lives here (see that module for the actual system probes, shared with
the TUI). The server itself always runs locally. `oa-mcp [host]` pins every
tool call to that one target (local if omitted) -- the counterpart to `oa
[host]`. `oa-mcp-multi` instead leaves the target per-call, gated by
--host-filter.

`--enable-plugins=odooly` adds the one non-read-only exception:
list_odooly_envs, instance_odooly_env, and the odooly plugin's scripts --
create_test_job, restore_app_icons, send_test_mail -- each its own tool,
matching a database against ~/odooly.ini and running the same actions that
plugin offers a human through the TUI's Toolbox, now offered to the agent
directly. It needs the extra installed
(`odoo-activity[odooly]`); the flag stays because a server exposing them to
an agent is a decision worth making explicitly, which installing a package
is not.

`--enable-plugins=pos` (which pulls in odooly too, same as the TUI) adds
pos_status: point-of-sale status for a database, the same data the TUI's
opt-in POS tab shows. Read-only, but gated the same way -- it still needs a
login.
"""

import functools
import inspect
import math
import os
import re
import signal
import stat
import subprocess
import time
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Annotated, Literal

import typer
from mcp.server.fastmcp import FastMCP
from typing_extensions import TypedDict

from odoo_activity import managers, plugins, probes
from odoo_activity.host import Host
from odoo_activity.probes import Instance, ProcRow, Worker

mcp = FastMCP("odoo-activity")
app = typer.Typer(add_completion=False)
app_multi = typer.Typer(add_completion=False)


# oa-mcp and oa-mcp-multi are "pinned vs multi" -- oa-mcp locks every tool
# to whatever host it was launched against (_pinned_target set), the
# counterpart to `oa host`; oa-mcp-multi leaves _pinned_target unset and
# gates per-call host with --host-filter instead.
_DEFAULT_HOST_FILE = Path.home() / ".ssh" / "config"
_WILDCARD = re.compile(r"[*?]")
_host_filter: re.Pattern[str] | None = None
_host_file: Path = _DEFAULT_HOST_FILE
_pinned_target: Host | None = None
# Launch-time only, never a per-call tool argument -- an agent must not be
# able to flip this on itself. Set solely by --include-sensitive-information
# on the oa-mcp/oa-mcp-multi command line, i.e. by the human starting the
# server.
_include_sensitive_information = False
# Same launch-time-only spirit as _include_sensitive_information, but the
# risk here is action rather than disclosure: odooly logs in and can write
# to the database it matches. Set solely by --enable-plugins.
_enabled_plugins: set[str] = set()


def _allowed(alias: str | None) -> bool:
    # local is always allowed; unset filter allows everything (odoo
    # dbfilter's "empty means no restriction" spirit). fullmatch, not
    # odoo's match: this gates real ssh access, a prefix match would let
    # "prod" filter through "prod-evil.example.com".
    return alias is None or _host_filter is None or bool(_host_filter.fullmatch(alias))


def _check_host(alias: str | None) -> None:
    if not _allowed(alias):
        msg = f"host {alias!r} rejected by --host-filter"
        raise ValueError(msg)


def _odooly() -> ModuleType:
    """The odooly plugin module, or a clear reason why it can't be used.

    Imported here rather than at module scope because there are two distinct
    ways to be unavailable and an agent should be told which: the extra is
    not installed, or the server was started without --enable-plugins=odooly.
    """
    try:
        from odoo_activity.plugins import odooly
    except ImportError as exc:
        msg = "odooly support unavailable -- install the extra: pip install 'odoo-activity[odooly]'"
        raise ValueError(msg) from exc

    if "odooly" not in _enabled_plugins:
        msg = "odooly support disabled -- restart the server with --enable-plugins=odooly"
        raise ValueError(msg)

    return odooly


def _pos() -> ModuleType:
    """The pos plugin module, or a clear reason why it can't be used --
    same two-way gate as `_odooly()`, and for the same reason: the extra
    might be missing, or the server might simply not have been started with
    --enable-plugins=pos.
    """
    try:
        from odoo_activity.plugins import pos
    except ImportError as exc:
        msg = "pos support unavailable -- install the extra: pip install 'odoo-activity[pos]'"
        raise ValueError(msg) from exc

    if "pos" not in _enabled_plugins:
        msg = "pos support disabled -- restart the server with --enable-plugins=pos"
        raise ValueError(msg)

    return pos


def _password_audit() -> ModuleType:
    """The password-audit plugin module, or a clear reason why it can't be
    used -- same two-way gate as `_odooly()`/`_pos()`.
    """
    try:
        from odoo_activity.plugins import password_audit
    except ImportError as exc:
        msg = "password-audit support unavailable -- the plugin is bundled, this is a bug"
        raise ValueError(msg) from exc

    if "password-audit" not in _enabled_plugins:
        msg = "password-audit disabled -- restart the server with --enable-plugins=password-audit"
        raise ValueError(msg)

    return password_audit


def _resolve_host(host: str | None, ssh_port: int | None) -> Host:
    if _pinned_target is not None:
        if host is not None and host != _pinned_target.alias:
            msg = f"host {host!r} rejected: this server is pinned to {_pinned_target.alias!r}"
            raise ValueError(msg)
        return _pinned_target

    _check_host(host)
    # Resolve aliases through the same file list_hosts reads them from, so a
    # custom --host-file is authoritative for connecting too -- not just for
    # enumeration. Left as the default (ssh's own ~/.ssh/config) when unset, so
    # a server started without --host-file keeps ssh's native behaviour and
    # never passes -F at a possibly-absent default config.
    config_file = _host_file if _host_file != _DEFAULT_HOST_FILE else None
    return Host(alias=host, port=ssh_port, config_file=config_file)


def _pinned_host(fn):
    """Resolve host/ssh_port into `target` for `fn`. `fn` takes `target: Host`
    keyword-only; rebuilds the public signature so FastMCP's schema still shows
    host/ssh_port."""
    sig = inspect.signature(fn, eval_str=True)
    params = [p for name, p in sig.parameters.items() if name != "target"]
    params += [
        inspect.Parameter("host", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None),
        inspect.Parameter("ssh_port", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=int | None),
    ]

    @functools.wraps(fn)
    def wrapper(*args, host=None, ssh_port=None, **kwargs):
        return fn(*args, target=_resolve_host(host, ssh_port), **kwargs)

    setattr(wrapper, "__signature__", sig.replace(parameters=params))  # noqa: B010
    return wrapper


def _ssh_config_aliases(path: Path) -> list[str]:
    """Literal (non-wildcard, non-negated) aliases from `Host` entries in an
    ssh config file. No `Include` following -- point --host-file at the
    file that actually lists them if it matters."""
    if not path.is_file():
        return []

    aliases = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        parts = line.split()
        if not parts or parts[0].lower() != "host":
            continue
        aliases.extend(p for p in parts[1:] if not p.startswith("!") and not _WILDCARD.search(p))

    return aliases


class HostStats(TypedDict):
    mem_pct: float
    swap_pct: float
    load_avg: tuple[float, float, float]
    uptime: float


class InstanceDatabases(TypedDict):
    databases: list[str]
    db_port: str | None
    neutralized: dict[str, bool]


class InstanceTop(TypedDict):
    odoo: list[ProcRow]
    postgres: list[ProcRow]


class StackDump(TypedDict):
    error: str
    workers: list[Worker]


DbQueryCommand = Literal["modules", "crons", "jobs", "users", "locks", "params", "check-sensitive-information"]
LogAnalysisCommand = Literal[
    "errors", "crons", "logins", "mails", "users", "usage", "passwords", "jobs", "workers", "calls"
]


# Discovery is 8 ssh round trips (~620ms remote) behind every tool. Only the
# sweep is cached; status is re-probed per call. uptime rides along, so it
# can be this stale.
_DISCOVERY_TTL = 30.0
_discovered: dict[tuple[str | None, int | None], tuple[float, list[Instance]]] = {}


def _instances(host: Host) -> list[Instance]:
    key = (host.alias, host.port)
    cached = _discovered.get(key)
    now = time.monotonic()

    if cached is None or now - cached[0] >= _DISCOVERY_TTL:
        _discovered[key] = (now, managers.list_instances(host))

    return _discovered[key][1]


def _with_status(inst: Instance, host: Host) -> Instance:
    return {**inst, "status": managers.instance_status(inst, host)}


def _find(name: str, host: Host) -> Instance | None:
    """The instance named `name` on `host`, or None — the lookup every
    per-instance tool below starts with."""
    return next((i for i in _instances(host) if i["name"] == name), None)


@mcp.tool()
@_pinned_host
def list_instances(*, target: Host) -> list[Instance]:
    """Every Odoo instance on `host` (systemd --user, supervisor, odoo.sh),
    with each instance's status corrected for a live process behind an
    ambiguous manager-reported "stopped".

    Args:
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    return [_with_status(inst, target) for inst in _instances(target)]


@mcp.tool()
@_pinned_host
def get_instance(name: str, *, target: Host) -> Instance | None:
    """A single instance by name (as reported by `list_instances`), or None
    if no instance with that name is currently known.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    return _with_status(inst, target) if inst else None


@mcp.tool()
@_pinned_host
def list_top(name: str, *, target: Host) -> list[ProcRow]:
    """The instance's master process plus every descendant worker, or an
    empty list if the instance isn't found or has no live process.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    return probes.procs_of(inst, target) if inst else []


@mcp.tool()
def list_hosts() -> list[str]:
    """Host aliases available for the `host` argument of the other tools:
    literal `Host` entries from --host-file (default ~/.ssh/config, wildcard
    and negated patterns skipped), narrowed by --host-filter."""
    if _pinned_target is not None:
        return [_pinned_target.alias] if _pinned_target.alias else []

    return [alias for alias in _ssh_config_aliases(_host_file) if _allowed(alias)]


@mcp.tool()
@_pinned_host
def host_stats(*, target: Host) -> HostStats | None:
    """Memory/swap/load/uptime for `host` itself, or None on a failed remote
    probe (a bad connection, not a parse bug — just skip this call).

    Args:
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    stats = probes.read_host_stats(target)
    if stats is None:
        return None

    _cpu_times, (mem_pct, swap_pct), load_avg, uptime = stats
    return {"mem_pct": mem_pct, "swap_pct": swap_pct, "load_avg": load_avg, "uptime": uptime}


@mcp.tool()
@_pinned_host
def instance_databases(name: str, *, target: Host) -> InstanceDatabases | None:
    """The instance's databases and the postgres port they live on, or None
    if the instance isn't found.

    `neutralized` maps each database to `database.is_neutralized` — what
    the database *claims*, as `odoo-db list` reports it. True means a copy
    that was neutralized: mail relays disabled, crons off.

    The claim is not proof. It is a config parameter, and a neutralization
    that died halfway — or a cron switched back on afterwards — leaves it
    set on a database that can still act on the outside world. Run
    `db_query(db, "check-sensitive-information")` before treating one as
    safe: it lists the module surfaces neutralize should have cleared and
    did not, plus the credentials neutralization never clears at all.

    A database missing from the map is one odoo-db could not read (postgres
    down, or not an odoo database) — unknown, never a guess either way. The
    map is empty when the host has no `odoo-db` installed.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return None

    databases, db_port = probes.databases_of(inst, target)
    # pg_target_of, not db_port: a docker instance's postgres also needs its
    # container address and credentials to be reachable at all.
    cluster = probes.neutralized_databases(probes.pg_target_of(inst, target), target) if databases else {}
    neutralized = {db: cluster[db] for db in databases if db in cluster}
    return {"databases": databases, "db_port": db_port, "neutralized": neutralized}


@mcp.tool()
@_pinned_host
def instance_top(name: str, *, target: Host) -> InstanceTop | None:
    """The instance's odoo top and its postgres backends, or None if
    the instance isn't found. Additive to `list_top`, which stays
    odoo-only.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return None

    odoo, postgres = probes.instance_procs(inst, target)
    return {"odoo": odoo, "postgres": postgres}


@mcp.tool()
@_pinned_host
def instance_version(name: str, *, target: Host) -> str | None:
    """The instance's Odoo version, or None if it's not found or the version
    can't be determined.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    return probes.instance_version(inst, target) if inst else None


@mcp.tool()
@_pinned_host
def instance_config(
    name: str,
    mode: Literal["compact", "expand"] = "compact",
    *,
    target: Host,
) -> str:
    """The instance's rendered odoo config: `compact` (only keys differing
    from odoo's default) or `expand` (every valid option filled in).

    Args:
        name: instance name as `list_instances` reports it.
        mode: `compact` or `expand`.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return "(no such instance)"

    path = probes.configfile_of(inst, target)
    if path is None:
        return "(no config file found)"

    version = probes.instance_version(inst, target)
    return probes.render_config(path, version, mode, target)


@mcp.tool()
@_pinned_host
def instance_log_tail(name: str, lines: int = 200, *, target: Host) -> str:
    """The last `lines` of the instance's odoo logfile.

    Args:
        name: instance name as `list_instances` reports it.
        lines: number of trailing lines to return.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return "(no such instance)"

    path = probes.logfile_of(inst, target)
    if path is None:
        return "(no logfile configured)"

    return probes.tail(path, lines, target)


@mcp.tool()
@_pinned_host
def instance_shell_command(name: str, *, target: Host) -> str | None:
    """The command to open an interactive `odoo shell` for the instance,
    reusing its live process's own venv/config -- to run yourself or
    suggest to the user as a next step. None if the instance isn't found
    or isn't running.

    ssh-wrapped when `host` is remote, so it's runnable as returned rather
    than needing `host` prefixed onto it by hand.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return None

    cmd = probes.shell_command(inst, target)
    return target.shell_invocation(cmd) if cmd else None


@mcp.tool()
@_pinned_host
def instance_dump_stacks(name: str, *, target: Host) -> StackDump:
    """SIGQUIT every process of the instance and parse the resulting stack
    dumps out of its log — a live thread/frame snapshot for diagnosing a
    stuck or spinning worker. `error` is non-empty (`workers` empty) only
    when there's nothing to show at all (no logfile, no workers, or nothing
    arrived).

    Each thread carries db/uid/url plus, on Odoo 17+, qc/qt/pt — a live
    snapshot at signal time, not an accumulator:
      qc = query_count, number of SQL queries so far this request.
      qt = query_time, SQL time so far this request, in seconds.
      pt = python_time, Odoo's own name for wall-clock-since-request-start
        minus qt — time spent outside the DB. Not "processing time".
    All are None when the thread isn't mid-request, or on pre-17 instances
    where qc/qt/pt were never reported.

    Check `idle` before reading qc/qt/pt: Odoo never clears a thread's
    request fields when it goes idle, so a thread back in its accept/wait
    loop (`idle: true` — innermost frame is a select/poll/sleep/wait) still
    shows its *last* request's numbers, not a live one. A large `pt` only
    means something on a non-idle thread.

    `workers`/`threads` are sorted busy-first (idle ones last), and each
    thread's `frames` are innermost-first — read frames[0], not frames[-1],
    for what a thread is doing right now.

    Args:
        name: instance name as `list_instances` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    # idle detection: probes._IDLE_FRAME_FUNCS / _IDLE_FRAME_PATH_MARKERS
    inst = _find(name, target)
    if inst is None:
        return {"error": "(no such instance)", "workers": []}

    error, workers = probes.dump_and_parse_stacks(inst, target)
    return {"error": error, "workers": probes.stacks_by_activity(workers)}


@mcp.tool()
@_pinned_host
def long_queries(db: str, port: str | None = None, *, target: Host) -> list[dict]:
    """Non-idle queries on `db`, longest-running first.

    Args:
        db: database name.
        port: postgres port, if the instance's cluster isn't the default one.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.

    Raises:
        ValueError: the query didn't run (postgres unreachable, ...) --
            an idle database answers an empty list instead.
    """
    rows, error = probes.long_queries(db, port, target)
    if rows is None:
        raise ValueError(error)

    return rows


@mcp.tool()
@_pinned_host
def db_query(
    db: str,
    command: DbQueryCommand,
    port: str | None = None,
    *,
    target: Host,
) -> list[dict] | str:
    """Run an `odoo-db` diagnostic command against `db` — a scoped subset:
    modules, crons, jobs, users, locks, params, check-sensitive-information.
    Stats/bloat/attachments/studio and the audit-oriented commands (they
    write a `$db.json` file to disk) are out of scope here.
    `check-sensitive-information` answers what a neutralized copy did not
    clear and what it still holds — the follow-up to `instance_databases`'
    `neutralized` claim. `params` returns `ir_config_parameter` rows, with
    secret-looking values masked by odoo-db unless the server was started
    with `--include-sensitive-information` — a launch-time-only choice, not
    a per-call argument here: no tool call can enable it on its own.

    Args:
        db: database name.
        command: modules, crons, jobs, users, locks, params, or
            check-sensitive-information.
        port: postgres port, if the instance's cluster isn't the default one.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    proc = probes.start_odoo_db(command, db, port, target, include_sensitive_information=_include_sensitive_information)
    if proc is None:
        return "(odoo-db not found on PATH)"

    try:
        result = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "(odoo-db timed out after 90s)"

    rows, raw = probes.parse_odoo_db_output(*result)
    return rows if rows is not None else raw


@mcp.tool()
@_pinned_host
def instance_log_analysis(
    name: str,
    command: LogAnalysisCommand,
    since: str | None = None,
    until: str | None = None,
    database: str | None = None,
    *,
    target: Host,
) -> list[dict] | str:
    """Run one of odoo-logs's 10 analyses against the instance's logfile and
    its rotated `.gz` siblings: errors, cron history, logins, outgoing mail,
    per-user activity, traffic usage, password changes, queue_job lifecycle,
    worker births/deaths, or request timing.

    Args:
        name: instance name as `list_instances` reports it.
        command: errors, crons, logins, mails, users, usage, passwords,
            jobs, workers, or calls.
        since: only entries at or after this (`YYYY-MM-DD` or
            `YYYY-MM-DD HH:MM:SS`, as written in the logs: usually UTC).
        until: only entries at or before this (same format; a bare date
            covers that whole day).
        database: only entries for this database -- an instance serves
            several, and without it every row mixes them. Lines whose
            database is unknown (`?`, e.g. a log line written before a
            request picks one) are kept, as they may belong to it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.

    Always give `since`/`until` on an instance with a lot of logs: without
    them every rotated file is read, which can exceed the memory limit the
    analysis runs under and return nothing. With a window only the files
    that overlap it are read (see `instance_log_files`).
    """
    inst = _find(name, target)
    if inst is None:
        return "(no such instance)"

    files = _log_files(inst, since, until, target)
    if isinstance(files, str):
        return files

    proc = probes.start_odoo_logs(command, files, target, since=since, until=until, database=database)
    if proc is None:
        return "(couldn't start odoo-logs)"

    try:
        result = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "(odoo-logs timed out after 90s)"

    rows, raw = probes.parse_odoo_db_output(*result)
    return rows if rows is not None else raw


def _log_files(inst, since: str | None, until: str | None, target: Host) -> list[Path] | str:
    """The instance's log files, narrowed to those overlapping `since`/`until`
    when given -- or a message the tool should return as-is."""
    files = probes.instance_log_files(inst, target)
    if not files:
        return "(no log file found)"
    if not (since or until):
        return files

    in_window = probes.files_in_window(files, since, until, target)
    if isinstance(in_window, str):
        return in_window

    return in_window or "(no log file covers that window)"


@mcp.tool()
@_pinned_host
def instance_log_files(
    name: str, since: str | None = None, until: str | None = None, *, target: Host
) -> list[dict] | str:
    """The instance's log files -- the current one and its rotated `.gz`
    siblings -- with the period each one covers, oldest first. Only the
    first and last entry of each file are read, so it is cheap even on
    gigabytes of logs: use it to find which files hold a time window before
    reading any log. Times are as written in the logs (Odoo logs UTC unless
    configured otherwise).

    Each row: `path`, `size` (bytes), `start`, `end` (timestamps) and `note`
    (`gz`, `truncated`, `empty`, `no timestamps`, `overlaps previous`).

    Args:
        name: instance name as `list_instances` reports it.
        since: only files that may hold entries at or after this
            (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`).
        until: only files that may hold entries at or before this (same
            format; a bare date covers that whole day).
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    inst = _find(name, target)
    if inst is None:
        return "(no such instance)"

    files = probes.instance_log_files(inst, target)
    if not files:
        return "(no log file found)"

    proc = probes.start_odoo_logs("list", files, target, since=since, until=until)
    if proc is None:
        return "(couldn't start odoo-logs)"

    try:
        result = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "(odoo-logs timed out after 90s)"

    if proc.returncode in (-signal.SIGKILL, 128 + signal.SIGKILL):
        return (
            "(odoo-logs list was killed, probably by the 60s CPU limit"
            " while caching .gz archives; calling again should continue from there)"
        )

    rows, raw = probes.parse_odoo_db_output(*result)
    return rows if rows is not None else raw


@mcp.tool()
@_pinned_host
def instance_error_traceback(
    name: str,
    error_type: str,
    error: str,
    since: str | None = None,
    until: str | None = None,
    database: str | None = None,
    *,
    target: Host,
) -> str:
    """The full traceback text behind one row of
    `instance_log_analysis(name, "errors")` -- that row only carries a count
    and first/last seen timestamps, never the traceback itself, since
    odoo-logs's `errors` command has no flag that puts one in its normal
    output; this runs a second, `--verbose`-backed call scoped to the exact
    entry the row came from.

    Args:
        name: instance name as `list_instances` reports it.
        error_type: the row's own `type` field, verbatim.
        error: the row's own `error` field, verbatim.
        since: only entries at or after this (`YYYY-MM-DD` or
            `YYYY-MM-DD HH:MM:SS`, as written in the logs: usually UTC).
        until: only entries at or before this (same format; a bare date
            covers that whole day).
        database: only entries for this database (as in
            `instance_log_analysis`).
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.

    Pass the same `since`/`until`/`database` as the `instance_log_analysis`
    call the row came from. The row's own `first`/`last` also work as
    bounds: they carry microseconds, which are cut to the second before
    they reach odoo-logs (whose `--from`/`--to` only accepts
    `YYYY-MM-DD[ HH:MM:SS]`). Without them every rotated file is read,
    which can exceed the memory limit and find nothing.
    """
    inst = _find(name, target)
    if inst is None:
        return "(no such instance)"

    since = since.split(",")[0].split(".")[0] if since else None
    until = until.split(",")[0].split(".")[0] if until else None

    files = _log_files(inst, since, until, target)
    if isinstance(files, str):
        return files

    return (
        probes.error_traceback(files, error_type, error, target, since=since, until=until, database=database)
        or "(no matching traceback found)"
    )


@mcp.tool()
@_pinned_host
def mail_audit(db: str, port: str | None = None, *, target: Host) -> dict | str:
    """Outbound mail config audit for `db`: whether the database is
    neutralized (the most common reason mail never leaves one), config
    parameters, per-company alias domains, company/OdooBot/admin addresses,
    outgoing mail servers (flagged if a known test-mail catcher or a known
    managed relay), and mass_mailing's install state — see odoo-db's own
    `mail` command.

    Kept out of `db_query`'s scoped command set rather than added there:
    odoo-db's `mail` answers one nested object, not the flat row list every
    `db_query` command shares — the same reason the TUI renders it through
    its own `panes/mail.py` instead of the generic table pane. Secret-looking
    values (`smtp_user`/`smtp_pass`) are masked by odoo-db unless the server
    was started with `--include-sensitive-information` — launch-time only,
    same rule `db_query`'s `params` follows.

    Args:
        db: database name.
        port: postgres port, if the instance's cluster isn't the default one.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    proc = probes.start_odoo_db("mail", db, port, target, include_sensitive_information=_include_sensitive_information)
    if proc is None:
        return "(odoo-db not found on PATH)"

    try:
        result = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "(odoo-db timed out after 90s)"

    rows, raw = probes.parse_odoo_db_output(*result)
    if rows is None:
        return raw
    return rows[0] if rows else {}


@mcp.tool()
def list_odooly_envs() -> list[str]:
    """Every environment section name in ~/odooly.ini -- what `env` on
    `create_test_job`, `restore_app_icons`, and `send_test_mail` accepts, and
    the set `instance_odooly_env` matches against. Requires
    --enable-plugins=odooly.
    """
    return [env["name"] for env in _odooly().read_odooly_envs()]


@mcp.tool()
def instance_odooly_env(name: str, db: str) -> str | None:
    """The odooly env (a section of ~/odooly.ini) that serves `db` on
    instance `name`, or None if none matches. Requires --enable-plugins=odooly.

    Always resolved locally against this machine's own ~/odooly.ini,
    independent of any `host`/`ssh_port` used to discover the instance
    itself -- odooly reaches the instance over the network, not over ssh.

    Args:
        name: instance name as `list_instances` reports it.
        db: database name, as `instance_databases` reports it.
    """
    odooly = _odooly()

    return odooly.match_odooly_env(name, db, odooly.read_odooly_envs(), odooly.read_odooly_aliases())


@mcp.tool()
def create_test_job(env: str) -> str:
    """Queue one of queue_job's own test jobs against `env`, to check whether
    a runner picks it up, and return what the script printed (stdout, then
    stderr). Requires --enable-plugins=odooly.

    Always local, even against a remote `host`: odooly reaches the instance
    over the network from wherever this server runs, using its own
    ~/odooly.ini -- never over ssh.

    Args:
        env: odooly env, e.g. from `instance_odooly_env` or
            `list_odooly_envs`.
    """
    return _odooly().run_odooly_script("create_test_job", env)


@mcp.tool()
def restore_app_icons(env: str) -> str:
    """Rewrite the apps-menu icons a restore-without-filestore leaves blank,
    on `env`, and return what the script printed (stdout, then stderr).
    Requires --enable-plugins=odooly.

    Always local, even against a remote `host`: odooly reaches the instance
    over the network from wherever this server runs, using its own
    ~/odooly.ini -- never over ssh.

    Args:
        env: odooly env, e.g. from `instance_odooly_env` or
            `list_odooly_envs`.
    """
    return _odooly().run_odooly_script("restore_app_icons", env)


@mcp.tool()
def send_test_mail(env: str, to: str) -> str:
    """Send one real email through mail.mail on `env`, to check outbound mail
    actually reaches an inbox, and return what the script printed (stdout,
    then stderr). Requires --enable-plugins=odooly.

    Always local, even against a remote `host`: odooly reaches the instance
    over the network from wherever this server runs, using its own
    ~/odooly.ini -- never over ssh.

    Args:
        env: odooly env, e.g. from `instance_odooly_env` or
            `list_odooly_envs`.
        to: recipient address.
    """
    if not to:
        msg = "send_test_mail needs `to` (recipient address)"
        raise ValueError(msg)
    return _odooly().run_odooly_script("send_test_mail", env, "--to", to)


@mcp.tool()
def pos_status(env: str) -> list[dict]:
    """Point-of-sale status on odooly env `env`: each pos.config's session
    status (closed, or open with its name and order count), when its latest
    session opened and its latest order was actually rung up, its payment
    methods and whether they wait for the terminal's own confirmation
    before moving on, and IoT Box/proxy/device settings. Requires
    --enable-plugins=pos (which pulls in odooly too).

    A field this instance's version doesn't have (an OCA module not
    installed, an older pos.config missing a newer field) is simply absent
    from a row rather than raising.

    Always local, even against a remote `host`: like odooly, pos reaches
    the instance over the network from wherever this server runs, using
    its own ~/odooly.ini -- never over ssh.

    Args:
        env: odooly env, e.g. from `instance_odooly_env` or
            `list_odooly_envs`.

    Raises:
        ValueError: pos support isn't enabled/installed, or `env` couldn't
            be reached (a missing section, a server that won't answer, ...).
    """
    rows, message = _pos().fetch_pos_status(env)
    if rows is None:
        raise ValueError(message)

    return rows


@mcp.tool()
@_pinned_host
def password_hashes(
    name: str,
    db: str,
    *,
    target: Host,
) -> str:
    """Bulk-export the active users' password hashes from `db` in hashcat
    mode 12100 format (sha512:rounds:b64salt:b64digest), written to a local
    file ready for `password_crack`. This is credential material: the answer
    is the file path, never the hashes themselves — treat the file like a
    password list, keep it off shared disks, delete it when done. Each call
    reserves a fresh private export directory using the host, instance and
    database identity; prior exports and their audit-local potfiles are not
    overwritten. The response also names the derived/tier-2 wordlists and
    .loginmap.json mapping. Delete the entire directory after the audit.

    Requires --enable-plugins=password-audit: the plugin is the gate, and
    enabling it is the operator's decision to export hashes at all.

    Args:
        name: instance name as `list_instances` reports it.
        db: database name, as `instance_databases` reports it.
        host: `[user@]hostname` to probe over ssh, or a ~/.ssh/config alias.
            Omit to probe the machine this server runs on.
        ssh_port: ssh port, if `host` is not on the default 22.
    """
    pa = _password_audit()
    inst = _find(name, target)
    if inst is None:
        msg = f"no such instance {name!r}"
        raise ValueError(msg)

    rows, error = pa.fetch_hash_rows(probes.pg_target_of(inst, target), db, target)
    if rows is None:
        raise ValueError(error)

    companies, _err = pa.fetch_company_names(probes.pg_target_of(inst, target), db, target)
    # Every export gets a fresh private namespace, including repeated calls.
    identity = target.alias or "local"
    if target.port is not None:
        identity += f":{target.port}"
    if target.container is not None:
        identity += f"/{target.container}"
    path = pa.export_path(name, db, identity)
    count = pa.write_hashcat_file(rows, path, companies=companies or [])
    return (
        f"{path} ({count} hashes, mode {pa.HASHCAT_MODE})\n"
        f"{path.with_suffix('.derived.txt')} (tier-1 wordlist: common/login/company/single-char -- try it first)\n"
        f"{path.with_suffix('.tier2.txt')} (tier-2 wordlist: 2-char combos and mutations -- when budget allows)\n"
        f"{path.with_suffix('.loginmap.json')} (login mapping for findings)\n"
        "Treat the entire export directory, including potfile and run artifacts, as credential material. "
        "Delete it when the audit is done."
    )


def _minutes_seconds(minutes: float) -> int:
    """Validate explicit budgets and round up to hashcat's integral seconds."""
    seconds = minutes * 60.0
    if not math.isfinite(seconds) or seconds <= 0:
        msg = "minutes must be finite and positive (with finite seconds)"
        raise ValueError(msg)
    return math.ceil(seconds)


class _AuditWork(TypedDict):
    label: str
    path: Path
    hashes: int
    groups: dict[tuple[int, str], int]


def _plan_records(path: Path, check):
    """Stream bounded regular inputs; check the shared deadline per record."""
    check()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        msg = f"missing or unreadable planner input/companion wordlist: {path}"
        raise ValueError(msg) from exc
    try:
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    total = 0
    with stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            msg = f"non-regular planner input/companion wordlist: {path}"
            raise ValueError(msg)
        if info.st_size > _PASSWORD_PLAN_MAX_BYTES:
            msg = f"planner input byte limit exceeded: {path}"
            raise ValueError(msg)
        for count in range(_PASSWORD_PLAN_MAX_RECORDS + 1):
            check()
            record = stream.readline(_PASSWORD_PLAN_MAX_RECORD_BYTES + 1)
            check()
            if not record:
                return
            total += len(record)
            if (
                count == _PASSWORD_PLAN_MAX_RECORDS
                or len(record) > _PASSWORD_PLAN_MAX_RECORD_BYTES
                or total > _PASSWORD_PLAN_MAX_BYTES
            ):
                msg = f"planner input record/byte limit exceeded: {path}"
                raise ValueError(msg)
            yield record.decode("utf-8").rstrip("\r\n")


def _plan_hash_groups(path: Path, check) -> dict[tuple[int, str], int]:
    groups: dict[tuple[int, str], set[str]] = {}
    for line in _plan_records(path, check):
        fields = line.strip().split(":")
        if len(fields) != 5 or fields[1] != "sha512" or not fields[3] or not fields[4]:
            continue
        try:
            rounds = int(fields[2])
        except ValueError:
            continue
        if rounds > 0:
            groups.setdefault((rounds, fields[3]), set()).add(fields[4])
    return {group: len(digests) for group, digests in groups.items()}


def _wordlist_sizes(entries: list[_AuditWork], tier: int, check) -> dict[Path, int]:
    """Keep wordlist sizes associated with export paths, never sorted positions."""
    sizes: dict[Path, int] = {}
    for entry in entries:
        wordlist = entry["path"].with_suffix(".derived.txt" if tier == 1 else ".tier2.txt")
        sizes[entry["path"]] = sum(1 for _record in _plan_records(wordlist, check))
    return sizes


def _candidate_seconds(entry: _AuditWork, candidates: int, speeds: dict[int, float]) -> float:
    """PBKDF2 runs once per candidate and distinct (rounds, salt), not digest."""
    return sum(candidates / speeds[rounds] for rounds, _salt in entry["groups"])


def _rounds_label(entry: _AuditWork) -> str:
    return "/".join(f"{rounds:,}" for rounds in sorted({r for r, _s in entry["groups"]}))


def _pass_call(entry: _AuditWork, tier: int, cost: float) -> str:
    wordlist = entry["path"].with_suffix(".derived.txt" if tier == 1 else ".tier2.txt")
    # Fund and cap complete integral seconds, including fractional estimates.
    runtime = max(1, math.ceil(cost))
    minutes = runtime / 60
    if _minutes_seconds(minutes) > runtime:
        minutes = math.nextafter(minutes, 0.0)
    return (
        f"   password_crack(hash_file={str(entry['path'])!r}, wordlist={str(wordlist)!r}, "
        f"minutes={minutes!r}) — runtime cap {runtime}s; before launching use "
        "min(this cap, floor(actual remaining audit time in seconds))/60; "
        "round minutes down if ceil(minutes*60) would exceed that cap; "
        "skip if less than 1s remains."
    )


def _allocate_tier1(
    entries: list[_AuditWork],
    sizes: dict[Path, int],
    speeds: dict[int, float],
    budget: float,
) -> tuple[float, float, list[_AuditWork], list[str], list[str]]:
    """Greedily fund complete tier-1 passes, largest unique hash count first."""
    remaining = budget
    plan: list[str] = []
    funded = 0.0
    funded_dbs: list[_AuditWork] = []
    skipped: list[str] = []
    for entry in sorted(entries, key=lambda e: -e["hashes"]):
        label = entry["label"]
        cost = max(1, math.ceil(_candidate_seconds(entry, sizes[entry["path"]], speeds)))
        if cost <= remaining:
            funded += cost
            remaining = max(0.0, remaining - cost)
            funded_dbs.append(entry)
            plan.append(
                f"1. tier 1 for {label} ({entry['hashes']} hashes, {_rounds_label(entry)} rounds): ~{cost:,.0f}s"
            )
            plan.append(_pass_call(entry, 1, cost))
        else:
            skipped.append(label)
            plan.append(f"   tier 1 for {label}: UNFUNDED — est ~{cost:,.0f}s exceeds the remaining budget")
    return funded, remaining, funded_dbs, skipped, plan


def _fit_tier2(
    entries: list[_AuditWork],
    remaining: float,
    speeds: dict[int, float],
    sizes: dict[Path, int],
) -> tuple[list[str], float]:
    """Fit funded databases' tier-2 passes, highest round count first."""
    plan: list[str] = []
    total = 0.0
    for entry in sorted(entries, key=lambda e: -max(r for r, _s in e["groups"])):
        label = entry["label"]
        cost = max(1, math.ceil(_candidate_seconds(entry, sizes[entry["path"]], speeds)))
        if total + cost <= remaining:
            total += cost
            plan.append(
                f"2. tier 2 for {label} ({entry['hashes']} hashes, {_rounds_label(entry)} rounds): ~{cost:,.0f}s"
            )
            plan.append(_pass_call(entry, 2, cost))
        else:
            plan.append(f"   tier 2 for {label}: SKIPPED — est ~{cost:,.0f}s exceeds remaining budget")
    return plan, total


_PASSWORD_PLAN_HEADROOM_S = 250.0
_PASSWORD_PLAN_RESERVE_S = 5.0
_PASSWORD_PLAN_MAX_FILES = 128
_PASSWORD_PLAN_MAX_BYTES = 32 * 1024 * 1024
_PASSWORD_PLAN_MAX_RECORDS = 200_000
_PASSWORD_PLAN_MAX_RECORD_BYTES = 16 * 1024


def _planning_remaining(started: float, budget: int, deadline: float) -> float:
    """Never start another probe, or publish a plan, after its deadline."""
    now = time.monotonic()
    remaining = deadline - now
    if remaining <= 0:
        elapsed = max(0.0, now - started)
        msg = (
            "planning budget/time limit exhausted; no valid plan: not all calibration/planning completed in time; "
            f"calibration + planning elapsed: {elapsed:,.0f}s (charged to budget); "
            f"audit remaining {max(0.0, budget - elapsed):,.0f}s. "
            "Retry with fewer round groups or a larger audit budget (planning is capped at 250s)."
        )
        raise ValueError(msg)
    return remaining


def _finalize_password_plan(
    lines: list[str],
    elapsed_index: int,
    tier2_index: int,
    started: float,
    budget: int,
    deadline: float,
    minutes: float,
    t1_funded: float,
    t2_total: float,
) -> str:
    """Publish only current accounting and still-funded passes before the deadline."""

    def accounting(elapsed: float) -> tuple[str, str, str]:
        available = max(0.0, budget - elapsed)
        return (
            f"{elapsed:,.0f}",
            f"{max(0.0, available - t1_funded):,.0f}",
            f"{max(0.0, available - t1_funded - t2_total):,.0f}",
        )

    while True:
        # Rebuild if output construction changed any displayed accounting.
        displayed = accounting(max(0.0, time.monotonic() - started))
        lines[elapsed_index] = f"calibration + planning elapsed: {displayed[0]}s (charged to budget)"
        lines[tier2_index] = f"tier 2 fit: {t2_total:,.0f}s of {displayed[1]}s remaining (funded dbs only)"
        lines[-1] = (
            f"budget: {minutes:g} min; tier1+tier2 est {t1_funded + t2_total:,.0f}s"
            f"; calibration+planning {displayed[0]}s"
            f" — leftover {displayed[2]}s"
            " may fund a generic wordlist pass (operator's call)"
        )
        result = "\n".join(lines)
        planning_remaining = _planning_remaining(started, budget, deadline)
        final_elapsed = max(0.0, deadline - started - planning_remaining)
        final_available = max(0.0, budget - final_elapsed)
        if t1_funded + t2_total > final_available:
            msg = (
                "planning budget invalidated; no valid plan: funded passes exceed the remaining audit budget; "
                f"calibration + planning elapsed: {final_elapsed:,.0f}s (charged to budget); "
                f"audit remaining {final_available:,.0f}s. Replan with a larger budget or fewer databases."
            )
            raise ValueError(msg)
        if accounting(final_elapsed) == displayed:
            return result


def _disambiguate_audit_labels(entries: list[_AuditWork]) -> None:
    labels = Counter(entry["label"] for entry in entries)
    for entry in entries:
        if labels[entry["label"]] > 1:
            entry["label"] = str(entry["path"])


def _smallest_salt_shape(entries: list[_AuditWork], rounds: int) -> int:
    return min(count for entry in entries if (count := sum(r == rounds for r, _s in entry["groups"])))


def _unsized_plan(
    entries: list[_AuditWork],
    cal_lines: list[str],
    unsized: list[int],
    started: float,
    budget: int,
) -> str:
    """Degrade to an unsized tier-1-only plan when calibration cannot finish.

    A slow GPU (e.g. an iGPU at 600k rounds) can fail every probe inside the
    250s MCP-call headroom. That is not an audit-stopping error:
    password_crack works without calibration, so emit every database's
    tier-1 pass in order without seconds estimates, skip tier 2, and let the
    operator reduce caps for launch delays against the audit deadline.
    """
    lines = [
        "calibration (conservative min(8, smallest actual per-round salt-group count) wordlist benchmark; "
        "measured rate used without throughput scaling): " + "; ".join(cal_lines)
    ]
    lines.append(
        "ESTIMATES UNAVAILABLE: calibration unavailable for "
        f"{', '.join(f'{r:,}' for r in unsized)} rounds; the plan is unsized. "
        "Run tier 1 in order, poll password_crack_status, and stop at the "
        "audit budget; tier 2 is skipped without measured rates."
    )
    elapsed = max(0.0, time.monotonic() - started)
    remaining = max(0, math.floor(budget - elapsed))
    lines.append(f"calibration + planning elapsed: {elapsed:,.0f}s (charged to budget)")
    lines.append(f"audit remaining: {remaining}s; unsized caps share this budget, not completion estimates.")
    for index, entry in enumerate(entries):
        cap = remaining // (len(entries) - index)
        if cap < 1:
            lines.append(f"   tier 1 for {entry['label']}: UNFUNDED — less than 1s allocated")
            continue
        remaining -= cap
        lines.append(f"1. tier 1 for {entry['label']} ({entry['hashes']} hashes, {_rounds_label(entry)} rounds)")
        lines.append(_pass_call(entry, 1, cap))
    return "\n".join(lines)


@mcp.tool()
def password_plan(
    audits: list[dict],
    minutes: float,
) -> str:
    """Build the time-budgeted crack plan for a password audit.

    `audits`: one entry per database, each `{"hash_file": <path as returned
    by `password_hashes`>}` — the tool reads each hash file to count hashes
    and their round counts (no db access needed at this stage).

    `minutes`: finite positive total time across all databases, rounded up
    to seconds. Calibration and planning wall time are charged to this budget.
    Planning is bounded by the smaller of this budget and 250 seconds of MCP
    headroom. Each calibration's timeout includes CPU preparation and the
    GPU probe. When calibration cannot finish inside that headroom (slow
    GPUs, e.g. an iGPU at 600k rounds), the plan degrades to an unsized
    tier-1-only result instead of aborting: run the tier-1 passes in order,
    with explicit caps sharing the remaining budget, poll, and stop at the
    budget; tier 2 is skipped without measured rates. Fractional pass estimates
    are rounded up to whole seconds for both funding and runtime caps.

    Calibrates each round count with min(8, smallest actual salt-group count)
    among its databases. The measured eight-salt rate is used conservatively
    for larger shapes, without fabricated throughput scaling. Costs
    use unique (rounds, salt) groups, not export rows or an
    unmeasured salt penalty. Tier 1 (`<db>.derived.txt`) is funded greedily,
    largest unique-hash count first; unfunded databases are named explicitly.
    Tier 2 (`<db>.tier2.txt`, combos and mutations as written) runs only for
    funded databases when it fits. Estimates do not guarantee elapsed attack
    time: process startup, polling and operator delay also consume real time.
    Run passes in the listed order with `password_crack`, polling each with
    `password_crack_status` until complete. A generic wordlist is optional
    only if actual time remains. Planning itself launches no audit attacks.

    Requires --enable-plugins=password-audit (same gate as the other
    password tools).
    """
    started = time.monotonic()
    pa = _password_audit()
    budget = _minutes_seconds(minutes)
    deadline = started + min(budget, _PASSWORD_PLAN_HEADROOM_S)

    check = functools.partial(_planning_remaining, started, budget, deadline)
    if len(audits) > _PASSWORD_PLAN_MAX_FILES:
        msg = f"planner hash-file limit exceeded: maximum {_PASSWORD_PLAN_MAX_FILES} exports"
        raise ValueError(msg)
    entries: list[_AuditWork] = []
    for a in audits:
        hf = Path(a["hash_file"])
        if not hf.is_file():
            msg = f"{a['hash_file']}: no such hash file"
            raise ValueError(msg)
        groups = _plan_hash_groups(hf, check)
        n_hashes = sum(groups.values())
        if n_hashes == 0:
            msg = f"{a['hash_file']}: no convertible hashes in the export — nothing to audit"
            raise ValueError(msg)
        entries.append({"label": hf.stem, "path": hf, "hashes": n_hashes, "groups": groups})

    if not entries:
        msg = "no hash files given"
        raise ValueError(msg)

    # Fresh export directories may contain identical filenames; keep them unambiguous.
    _disambiguate_audit_labels(entries)

    t1_sizes = _wordlist_sizes(entries, 1, check)
    t2_sizes = _wordlist_sizes(entries, 2, check)

    # calibrate at EVERY distinct round count in the audit (a mixed fleet —
    # e.g. a v9 db at 19k rounds beside modern 600k dbs — has very
    # different per-round throughput; one flat rate misestimates the
    # low-round dbs). Same attack shape as the real passes; no scaling
    # arithmetic (throughput is latency-bound, not round-proportional).
    # Cap the smallest actual shape at calibrate's eight-salt maximum;
    # retain that measured rate conservatively for larger actual shapes.
    speeds: dict[int, float] = {}
    cal_lines = []
    unsized: list[int] = []
    round_counts = sorted({r for entry in entries for r, _s in entry["groups"]})
    for rounds in round_counts:
        remaining = _planning_remaining(started, budget, deadline)
        smallest_actual = _smallest_salt_shape(entries, rounds)
        n_salts = min(8, smallest_actual)
        reserve = min(_PASSWORD_PLAN_RESERVE_S, remaining / 2)
        speed, cal_err = pa.calibrate(rounds, timeout_s=remaining - reserve, n_salts=n_salts)
        if speed is None:
            # Calibration can legitimately fail on slow GPUs (e.g. an iGPU
            # cannot finish one 600k-round probe inside the 250s MCP-call
            # headroom). That is not an audit-stopping error: password_crack
            # works without calibration, so degrade to an unsized tier-1-only
            # plan instead of aborting the whole flow.
            unsized.extend(r for r in round_counts if r not in speeds)
            cal_lines.append(f"unmeasured at {rounds:,} rounds ({cal_err})")
            break
        _planning_remaining(started, budget, deadline)
        speeds[rounds] = speed
        cal_lines.append(
            f"{speed:,.0f} candidate-salt pairs/s at {rounds:,} rounds "
            f"({n_salts} synthetic salts; smallest actual salt-group count {smallest_actual})"
        )

    lines = [
        "calibration (conservative min(8, smallest actual per-round salt-group count) wordlist benchmark; "
        "measured rate used without throughput scaling): " + "; ".join(cal_lines)
    ]
    if unsized:
        return _unsized_plan(entries, cal_lines, unsized, started, budget)
    lines.extend(f"database {entry['label']}: {entry['path']}" for entry in entries)
    lines.append(
        "Execution: never omit minutes (the independent 250s default is not the plan). "
        "Poll password_crack_status until each pass completes; charge startup, polling and delays "
        "against the original audit deadline, and cap every later pass to actual remaining audit time."
    )

    # Charge measured call overhead, including calibration setup and file reads.
    _planning_remaining(started, budget, deadline)
    elapsed = max(0.0, time.monotonic() - started)
    available = max(0.0, budget - elapsed)
    elapsed_index = len(lines)
    lines.append(f"calibration + planning elapsed: {elapsed:,.0f}s (charged to budget)")
    t1_funded, remaining, funded_dbs, skipped_dbs, plan = _allocate_tier1(entries, t1_sizes, speeds, available)
    lines.append(f"tier 1 (prioritized): ~{t1_funded:,.0f}s funded across {len(funded_dbs)}/{len(entries)} dbs")
    if skipped_dbs:
        lines.append(f"BUDGET: {len(skipped_dbs)} dbs unfunded for tier-1: {', '.join(skipped_dbs)}")
        lines.append("Run another audit with a bigger budget (or fewer dbs) to cover them.")

    t2_plan, t2_total = _fit_tier2(funded_dbs, remaining, speeds, t2_sizes)
    plan += t2_plan
    tier2_index = len(lines)
    lines.append(f"tier 2 fit: {t2_total:,.0f}s of {remaining:,.0f}s remaining (funded dbs only)")
    lines += plan
    lines.append("")
    return _finalize_password_plan(
        lines,
        elapsed_index,
        tier2_index,
        started,
        budget,
        deadline,
        minutes,
        t1_funded,
        t2_total,
    )


@mcp.tool()
def password_crack(
    hash_file: str,
    wordlist: str | None = None,
    rules: str | None = None,
    minutes: float | None = None,
) -> str:
    """Run hashcat locally on this machine's GPU against a file produced by
    `password_hashes` (mode 12100, PBKDF2-HMAC-SHA512 as Odoo 16+ stores).
    With `wordlist`: launch a detached dictionary attack (optionally with
    a rule file) under a 250-second default budget — the audit answers
    "what could an HTTP bruteforce scanner find in a sitting", not a full
    audit; poll `password_crack_status` until complete before another pass.
    Hashcat stops at its runtime budget; process setup adds wall time.
    Without `wordlist`: bounded `--show`, i.e. previous audit-local cracks.

    Requires --enable-plugins=password-audit. Runs on the machine this MCP
    server runs on — point it at a GPU host by running the server there.

    Args:
        hash_file: path as returned by `password_hashes`.
        wordlist: wordlist path, e.g. the derived wordlist `password_hashes`
            wrote, or rockyou.txt. Omit to run `--show`: re-list what
            previous passes already cracked (the audit-local potfile) — no
            new cracking happens without a wordlist.
        rules: hashcat rules file applied to the wordlist (needs wordlist).
        minutes: finite positive runtime override, rounded up to seconds
            (even tiny positive budgets become one second). Omit for 250s.
            All wordlist passes launch detached; poll `password_crack_status`.
    """
    pa = _password_audit()
    path = Path(hash_file)
    if not path.is_file():
        msg = f"{hash_file}: no such file"
        raise ValueError(msg)

    hits, error = pa.run_hashcat(
        path,
        wordlist=Path(wordlist) if wordlist else None,
        rules=Path(rules) if rules else None,
        runtime_budget=_minutes_seconds(minutes) if minutes is not None else pa.RUNTIME_BUDGET_S,
    )
    if error:
        raise ValueError(error)

    return hits or "(no cracks yet)"


@mcp.tool()
def password_findings(hash_file: str) -> str:
    """Consolidated cracks for a `password_hashes` export as STRUCTURED
    JSON: [{"uid", "login", "digest", "password", "source"}], joined from
    the audit-local potfile and the hash file. Use this for report
    aggregation instead of parsing prose answers — it is the
    machine-readable end of the audit flow, and it resolves the
    audit-local-vs-global potfile question server-side (the audit-local
    potfile is canonical).

    Requires --enable-plugins=password-audit (same gate as the other
    password tools).
    """
    pa = _password_audit()
    path = Path(hash_file)
    if not path.is_file():
        msg = f"{hash_file}: no such file"
        raise ValueError(msg)

    findings, error = pa.password_findings(path)
    if error:
        raise ValueError(error)

    import json

    return json.dumps(findings, indent=2)


@mcp.tool()
def password_crack_status(hash_file: str) -> str:
    """Where a detached wordlist pass launched by `password_crack` stands.
    Poll this instead of re-calling `password_crack`:
    while hashcat runs it answers "running, N cracks so far, ~Xs of budget
    left"; once it finishes it returns the cracks and consumes the status.
    A launch whose MCP call died still keeps cracking — this is how you
    collect that work.

    Requires --enable-plugins=password-audit (same gate as the other
    password tools).
    """
    pa = _password_audit()
    path = Path(hash_file)
    if not path.is_file():
        msg = f"{hash_file}: no such file"
        raise ValueError(msg)

    answer, error = pa.crack_status(path)
    if error:
        raise ValueError(error)

    return answer


@app.command()
def main(
    host: str | None = typer.Argument(
        None, help="[user@]host to pin this server to, e.g. openerp@demo. Omit for local."
    ),
    port: int | None = typer.Option(None, "--port", "-p", help="ssh port, for a non-default one."),
    transport: Literal["stdio", "streamable-http"] = typer.Option(
        "stdio", "--transport", help="stdio (spawned by the client) or streamable-http (network server)"
    ),
    bind_host: str = typer.Option("127.0.0.1", "--bind-host", help="streamable-http only"),
    bind_port: int = typer.Option(8000, "--bind-port", help="streamable-http only"),
    include_sensitive_information: bool = typer.Option(
        False,
        "--include-sensitive-information",
        help="Let db_query's `params` command return secret-looking values unmasked. Launch-time only "
        "-- no tool call can turn this on itself; set it only if every caller of this server should see "
        "plaintext secrets.",
    ),
    enable_plugins: Annotated[
        list[str] | None,
        typer.Option(
            "--enable-plugins",
            help="Plugins (comma-separated, or repeat the flag) whose gated tools should be exposed, "
            "e.g. --enable-plugins=odooly to expose list_odooly_envs/instance_odooly_env/create_test_job/"
            "restore_app_icons/send_test_mail, or --enable-plugins=pos (pulls in odooly too) to also expose "
            "pos_status -- matching databases "
            "against ~/odooly.ini the same way `oa --enable-plugins=...` does. Launch-time only -- no tool "
            "call can turn this on itself; set it only if the agent should be able to log in and reach a "
            "matched database. Omit to expose none of them.",
        ),
    ] = None,
) -> None:
    """oa-mcp — MCP server exposing odoo-activity's read-only capacities,
    pinned to a single host: the counterpart to `oa host`, so a human on
    `oa host` and their agent on `oa-mcp host` are always looking at the
    same target. Omit `host` to pin to local, matching bare `oa`."""
    global _pinned_target, _include_sensitive_information, _enabled_plugins
    _pinned_target = Host(alias=host, port=port)
    _include_sensitive_information = include_sensitive_information
    names = plugins.split_names(enable_plugins)
    if names:
        installed, _ = plugins.load()
        try:
            active = plugins.select(installed, names, [])
        except plugins.UnknownPlugin as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        _enabled_plugins = {plugin.name for plugin in active}
    else:
        # select() treats an empty `enable` as "everything installed" -- the
        # right default for oa, but oa-mcp is opt-in only: omitting the flag
        # must expose none of these tools, not all of them.
        _enabled_plugins = set()

    if transport == "streamable-http":
        mcp.settings.host = bind_host
        mcp.settings.port = bind_port

    mcp.run(transport=transport)


@app_multi.callback(invoke_without_command=True)
def main_multi(
    transport: Literal["stdio", "streamable-http"] = typer.Option(
        "stdio", "--transport", help="stdio (spawned by the client) or streamable-http (network server)"
    ),
    bind_host: str = typer.Option("127.0.0.1", "--bind-host", help="streamable-http only"),
    bind_port: int = typer.Option(8000, "--bind-port", help="streamable-http only"),
    host_filter: str = typer.Option(
        "",
        "--host-filter",
        help="regex (odoo dbfilter-style); only ssh targets it fullmatches are reachable. Unset: no restriction.",
    ),
    host_file: Annotated[
        Path,
        typer.Option(
            "--host-file",
            help="ssh config to read literal Host aliases from, for list_hosts(). Also passed as `ssh -F` "
            "when connecting, so the file aliases are listed from is the one they resolve through.",
        ),
    ] = _DEFAULT_HOST_FILE,
    include_sensitive_information: bool = typer.Option(
        False,
        "--include-sensitive-information",
        help="Let db_query's `params` command return secret-looking values unmasked. Launch-time only "
        "-- no tool call can turn this on itself; set it only if every caller of this server should see "
        "plaintext secrets.",
    ),
    enable_plugins: Annotated[
        list[str] | None,
        typer.Option(
            "--enable-plugins",
            help="Plugins (comma-separated, or repeat the flag) whose gated tools should be exposed, "
            "e.g. --enable-plugins=odooly to expose list_odooly_envs/instance_odooly_env/create_test_job/"
            "restore_app_icons/send_test_mail, or --enable-plugins=pos (pulls in odooly too) to also expose "
            "pos_status -- matching databases "
            "against ~/odooly.ini the same way `oa --enable-plugins=...` does. Launch-time only -- no tool "
            "call can turn this on itself; set it only if the agent should be able to log in and reach a "
            "matched database. Omit to expose none of them.",
        ),
    ] = None,
) -> None:
    """oa-mcp-multi — same tools as oa-mcp, capped by --host-filter."""
    global _host_filter, _host_file, _include_sensitive_information, _enabled_plugins
    if transport == "streamable-http":
        mcp.settings.host = bind_host
        mcp.settings.port = bind_port

    _host_filter = re.compile(host_filter) if host_filter else None
    _host_file = host_file
    _include_sensitive_information = include_sensitive_information
    names = plugins.split_names(enable_plugins)
    if names:
        installed, _ = plugins.load()
        try:
            active = plugins.select(installed, names, [])
        except plugins.UnknownPlugin as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        _enabled_plugins = {plugin.name for plugin in active}
    else:
        # select() treats an empty `enable` as "everything installed" -- the
        # right default for oa, but oa-mcp is opt-in only: omitting the flag
        # must expose none of these tools, not all of them.
        _enabled_plugins = set()

    mcp.run(transport=transport)


if __name__ == "__main__":
    # Stay pointing at single-host, dev-run default
    app()
