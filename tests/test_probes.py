import configparser
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pyperclip

from odoo_activity import managers, probes
from odoo_activity.host import Host
from odoo_activity.probes import Instance

_INSTANCE: Instance = {"name": "demo", "status": "running", "uptime": "0:01:00", "manager": "systemd"}


def _parser(options: dict[str, str]) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()
    parser.add_section("options")
    for key, value in options.items():
        parser.set("options", key, value)
    return parser


def _fake_procs(cmd):
    return lambda *_: [{"pid": "1", "ppid": "0", "user": "odoo", "mem": "0.1", "nice": "0", "cmd": cmd}]


def _no_ssh_tty(monkeypatch):
    # isolate from whatever terminal *this* test happens to run under --
    # these tests are about the target `Host`, not odoo-activity's own tty
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)


def test_copy_shell_command_local(monkeypatch):
    """Local host: the raw `odoo shell` command is copied as-is."""
    _no_ssh_tty(monkeypatch)
    monkeypatch.setattr(probes, "procs_of", _fake_procs("/venv/bin/python3 /opt/odoo/odoo-bin -c /etc/odoo.conf"))
    copied = {}
    monkeypatch.setattr(pyperclip, "copy", lambda text: copied.setdefault("text", text))

    host = Host()
    cmd = probes.shell_command(_INSTANCE, host)
    assert cmd is not None
    text = host.shell_invocation(cmd)

    assert probes.try_local_clipboard(text) is True
    assert copied["text"] == "/venv/bin/python3 /opt/odoo/odoo-bin shell --no-http -c /etc/odoo.conf"


def test_copy_shell_command_remote(monkeypatch):
    """Remote host (--host): the command only exists on that host's own
    filesystem, so what gets copied must be an ssh invocation the user can
    paste into their own local terminal to actually reach it -- not the
    bare remote command, which would just fail to run locally."""
    _no_ssh_tty(monkeypatch)
    monkeypatch.setattr(probes, "procs_of", _fake_procs("/venv/bin/python3 /opt/odoo/odoo-bin -c /etc/odoo.conf"))
    copied = {}
    monkeypatch.setattr(pyperclip, "copy", lambda text: copied.setdefault("text", text))

    host = Host(alias="prod", port=2222)
    cmd = probes.shell_command(_INSTANCE, host)
    assert cmd is not None
    text = host.shell_invocation(cmd)

    assert probes.try_local_clipboard(text) is True
    assert (
        copied["text"] == "ssh -t -p 2222 prod '/venv/bin/python3 /opt/odoo/odoo-bin shell --no-http -c /etc/odoo.conf'"
    )


def test_shell_command_resolves_bare_interpreter(monkeypatch):
    """A bare argv[0] only resolves in the process's own launch context: the
    venv it was found in wins, since `/proc/<pid>/exe` resolves through the
    venv symlink to the base interpreter; `exe` is the fallback. A relative
    argv[0] resolves against the process's cwd."""
    monkeypatch.setattr(probes, "procs_of", _fake_procs("python3 /opt/odoo/odoo-bin -c /etc/odoo.conf"))
    monkeypatch.setattr(probes, "_exe_of", lambda *_: "/usr/bin/python3.10")
    monkeypatch.setattr(probes, "_environ_of", lambda *_: {"VIRTUAL_ENV": "/venv"})
    monkeypatch.setattr(Host, "is_file", lambda self, path: path == "/venv/bin/python3")

    tail = "/opt/odoo/odoo-bin shell --no-http -c /etc/odoo.conf"
    assert probes.shell_command(_INSTANCE, Host()) == f"/venv/bin/python3 {tail}"

    monkeypatch.setattr(probes, "_environ_of", lambda *_: {})
    assert probes.shell_command(_INSTANCE, Host()) == f"/usr/bin/python3.10 {tail}"

    monkeypatch.setattr(probes, "_proc_link", lambda *_: "/opt/odoo")
    assert probes._resolve_argv0("./odoo-bin", "1", Host()) == "/opt/odoo/odoo-bin"


def _argv_inst(command: str, pid: str = "") -> Instance:
    """An instance with no odoo.conf — argv is the only config it has."""
    return {
        "name": "demo",
        "status": "running",
        "uptime": "-",
        "manager": "local",
        "command": command,
        "directory": "/nonexistent",
        "config": "",
        "pid": pid,
    }


def test_argv_reads_back_as_config(monkeypatch):
    """A directly-run instance may never touch odoo.conf, so its argv has to
    read back through the same parser and accessors every other manager uses
    — both `--k=v` and `--k v`, short flags under their config key."""
    inst = _argv_inst("odoo-bin -d demo --http-port=8070 --logfile /var/log/odoo.log --db_port 5434 -s")

    _, parser = probes.instance_config(inst, Host())

    assert probes._opt(parser, "db_name") == "demo"
    assert probes._opt(parser, "http_port") == "8070"
    assert probes.db_port_of(inst, Host()) == "5434"
    assert probes.logfile_of(inst, Host()) == Path("/var/log/odoo.log")

    # `-d` names the one db it serves; only an unpinned runner lists its role's
    monkeypatch.setattr(probes, "databases_by_role", lambda *_, **__: ["unrelated", "other"])
    assert probes.databases_of(inst, Host()) == (["demo"], "5434")
    assert probes.databases_of(_argv_inst("odoo-bin --addons-path /a"), Host())[0] == ["unrelated", "other"]


def test_logfile_falls_back_to_redirected_stdout(monkeypatch):
    """No `logfile` anywhere: a `> server.log` redirect is still tailable, a
    terminal is not — Log and Stacks stay empty for that instance."""
    inst = _argv_inst("odoo-bin -d demo", pid="100")

    monkeypatch.setattr(probes, "_proc_link", lambda *_: "/home/x/server.log")
    assert probes.logfile_of(inst, Host()) == Path("/home/x/server.log")

    monkeypatch.setattr(probes, "_proc_link", lambda *_: "/dev/pts/5")
    assert probes.logfile_of(inst, Host()) is None


def test_instance_log_files_orders_rotations_numerically(tmp_path):
    """`.9` has to sort before `.10` — plain name order would get that
    backwards."""
    logfile = tmp_path / "server.log"
    logfile.write_text("current")
    for rotation in (1, 9, 10, 2):
        suffix = "" if rotation == 1 else ".gz"
        (tmp_path / f"server.log.{rotation}{suffix}").write_text("old")

    inst = _argv_inst(f"odoo-bin -d demo --logfile {logfile}")

    assert probes.instance_log_files(inst, Host()) == [
        logfile,
        tmp_path / "server.log.1",
        tmp_path / "server.log.2.gz",
        tmp_path / "server.log.9.gz",
        tmp_path / "server.log.10.gz",
    ]


def test_instance_log_files_empty_when_logfile_missing(tmp_path):
    """A configured `logfile` that was never actually created — handing
    odoo-logs a missing path would refuse the whole command."""
    inst = _argv_inst(f"odoo-bin -d demo --logfile {tmp_path / 'server.log'}")

    assert probes.instance_log_files(inst, Host()) == []


def test_row_matches_values_only_case_insensitive():
    row = {"key": "database.secret", "value": "********"}
    assert probes.row_matches(row, "SECRET") is True  # case-insensitive
    assert probes.row_matches(row, "key") is False  # column *names* never match
    assert probes.row_matches({"value": None}, "any") is False  # SQL NULL doesn't raise


def test_start_odoo_db_builds_params_argv(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: captured.update(cmd=cmd))

    probes.start_odoo_db("params", "demo", None, Host())
    assert captured["cmd"] == ["odoo-db", "--output-format", "json", "params", "demo"]  # no --include-code

    probes.start_odoo_db("params", "demo", "5433", Host())
    assert captured["cmd"] == ["env", "PGPORT=5433", "odoo-db", "--output-format", "json", "params", "demo"]

    # a *global* odoo-db option: after the subcommand Typer would reject it
    probes.start_odoo_db("params", "demo", None, Host(), include_sensitive_information=True)
    assert captured["cmd"] == [
        "odoo-db",
        "--output-format",
        "json",
        "--include-sensitive-information",
        "params",
        "demo",
    ]


def test_start_odoo_db_asks_for_every_row_only_when_told(monkeypatch):
    """`--all` is what brings the inactive/uninstalled rows (and the status
    column the TUI filters on) — and only odoo-db commands that have the flag
    may be handed it."""
    seen: list[list[str]] = []
    monkeypatch.setattr(Host, "popen", lambda self, argv, **_: seen.append(argv) or "proc")

    for command in ("crons", "modules", "users"):
        probes.start_odoo_db(command, "demo", include_inactive=True)
        assert "--all" in seen[-1], command

        probes.start_odoo_db(command, "demo")
        assert "--all" not in seen[-1], command

    # a command with no such flag must never be handed it
    probes.start_odoo_db("locks", "demo", include_inactive=True)
    assert "--all" not in seen[-1]


def test_start_odoo_logs_wraps_in_a_resource_limited_shell(monkeypatch):
    """`RLIMIT_AS`/`RLIMIT_CPU` are per-process, so the cap has to be on the
    subprocess itself, via a wrapping shell (works the same over ssh)."""
    seen: list[list[str]] = []
    monkeypatch.setattr(Host, "popen", lambda self, argv, **_: seen.append(argv) or "proc")

    probes.start_odoo_logs("errors", [Path("/var/log/server.log")], Host())

    assert seen[-1][:2] == ["sh", "-c"]
    wrapped = seen[-1][2]
    assert "ulimit -t 60;" in wrapped
    assert "ulimit -v 1048576;" in wrapped
    assert wrapped.endswith("exec odoo-logs --output-format json errors /var/log/server.log")


def test_start_odoo_logs_passes_every_file(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(Host, "popen", lambda self, argv, **_: seen.append(argv) or "proc")

    probes.start_odoo_logs("crons", [Path("/var/log/server.log"), Path("/var/log/server.log.1.gz")], Host())

    assert seen[-1][2].endswith(
        "exec odoo-logs --output-format json crons /var/log/server.log /var/log/server.log.1.gz"
    )


def test_start_odoo_logs_with_verbose_and_extra_flags(monkeypatch):
    """`--verbose` is a global option -- it must land before the subcommand;
    `extra` (e.g. `--traceback-only`) lands after it, before the files."""
    seen: list[list[str]] = []
    monkeypatch.setattr(Host, "popen", lambda self, argv, **_: seen.append(argv) or "proc")

    probes.start_odoo_logs(
        "errors",
        [Path("/var/log/server.log")],
        Host(),
        verbose_file="/tmp/oa-errors-1-abc.log",
        extra=("--traceback-only",),
    )

    assert seen[-1][2].endswith(
        "exec odoo-logs --verbose /tmp/oa-errors-1-abc.log --output-format json "
        "errors --traceback-only /var/log/server.log"
    )


def test_matching_traceback_blocks_reverses_the_squashed_id():
    """`error` comes off the grouped row already squashed ("(N)" instead of
    a real pid) -- the search must still find the real pid in raw text."""
    dumped = (
        "2026-01-01 10:00:00,000 123 ERROR demo odoo.addons.base.models.ir_cron: "
        "Job 'long cron' (3222624) server action #12 failed\n"
        "Traceback (most recent call last):\n"
        "  File demo.py, line 1\n"
        "odoo.addons.base.models.ir_cron: Job 'long cron' (3222624) server action #12 failed\n"
        "2026-01-01 11:00:00,000 124 ERROR demo odoo.modules.loading: "
        "Some modules are not loaded\n"
        "unrelated block\n"
    )

    result = probes._matching_traceback_blocks(
        dumped, "odoo.addons.base.models.ir_cron", "Job 'long cron' (N) server action #12 failed"
    )

    assert "3222624" in result
    assert "unrelated block" not in result


def test_matching_traceback_blocks_no_match_is_empty():
    dumped = "2026-01-01 10:00:00,000 123 ERROR demo odoo.modules.loading: something else\n"
    assert probes._matching_traceback_blocks(dumped, "KeyError", "'socket'") == ""


def test_matching_traceback_blocks_empty_dump_is_empty():
    """Regression guard: `--traceback-only` can legitimately keep nothing at
    all (every entry in scope was a single-line ERROR with no Traceback --
    real staging logs hit this for e.g. `odoo.modules.loading`/`ir_model`
    entries) -- `zip(starts, [*starts[1:], len(text)], strict=True)` used to
    raise ValueError on an empty `starts` instead of returning ""."""
    assert probes._matching_traceback_blocks("", "KeyError", "'socket'") == ""


def test_error_traceback_end_to_end(monkeypatch, tmp_path):
    """Orchestration: start_odoo_logs gets the right verbose/extra args, the
    dumped file is read back and cleaned up, and the result is filtered."""
    dumped_file = tmp_path / "dumped.log"
    dumped_file.write_text(
        "2026-01-01 10:00:00,000 1 ERROR demo x: y\nKeyError: 'socket'\n"
        "2026-01-01 10:05:00,000 1 ERROR demo x: y\nValueError: nope\n"
    )

    seen_start_kwargs = {}
    rm_calls = []

    class _FakeProc:
        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            pass

    def fake_start_odoo_logs(command, files, host, *, verbose_file: str, extra=()):
        seen_start_kwargs["command"] = command
        seen_start_kwargs["files"] = files
        seen_start_kwargs["verbose_file"] = verbose_file
        seen_start_kwargs["extra"] = extra
        # simulate odoo-logs having written the verbose file
        Path(verbose_file).write_text(dumped_file.read_text())
        return _FakeProc()

    def fake_run(self, argv, **_):
        rm_calls.append(argv)
        if argv[:2] == ["rm", "-f"]:
            Path(argv[2]).unlink(missing_ok=True)  # keep the test's real /tmp file clean
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(probes, "start_odoo_logs", fake_start_odoo_logs)
    monkeypatch.setattr(Host, "run", fake_run)

    result = probes.error_traceback([Path("/var/log/server.log")], "KeyError", "'socket'", Host())

    assert seen_start_kwargs["command"] == "errors"
    assert seen_start_kwargs["extra"] == ("--traceback-only",)
    assert seen_start_kwargs["verbose_file"].startswith("/tmp/oa-errors-")
    assert "KeyError: 'socket'" in result
    assert "ValueError" not in result
    assert rm_calls and rm_calls[0][:2] == ["rm", "-f"]  # verbose file cleaned up


def test_error_traceback_no_proc_is_empty(monkeypatch):
    monkeypatch.setattr(probes, "start_odoo_logs", lambda *_a, **_k: None)
    assert probes.error_traceback([Path("/var/log/server.log")], "KeyError", "'socket'", Host()) == ""


def test_error_traceback_cleans_up_the_verbose_file_on_timeout(monkeypatch):
    """Regression guard: the cleanup rm used to live in a `finally` around
    just the read step, so a `communicate()` timeout returned early and
    never ran it, leaking the temp file."""
    rm_calls = []

    class _FakeProc:
        def communicate(self, timeout: float = 90) -> None:
            raise subprocess.TimeoutExpired(cmd="odoo-logs", timeout=timeout)

        def kill(self):
            pass

    monkeypatch.setattr(probes, "start_odoo_logs", lambda *_a, **_k: _FakeProc())
    monkeypatch.setattr(Host, "run", lambda self, argv, **_: rm_calls.append(argv) or SimpleNamespace(stdout=""))

    result = probes.error_traceback([Path("/var/log/server.log")], "KeyError", "'socket'", Host())

    assert result == ""
    assert rm_calls and rm_calls[0][:2] == ["rm", "-f"]


# --- docker ---------------------------------------------------------------

_DOCKER_INSTANCE: Instance = {
    "name": "acme",
    "status": "running",
    "uptime": "0:01:00",
    "manager": "docker",
    "container": "acme-odoo-1",
    "db_container": "acme-db-1",
    "workdir": "/srv/acme",
    "command": "/opt/odoo/common/entrypoint odoo --workers=2",
}


def _recorder(monkeypatch, stdout="", returncode=0):
    """Record every argv a Host runs, answering all of them the same way."""
    calls: list[list[str]] = []

    def fake_run(self, argv, input_text=None):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(Host, "run", fake_run)
    return calls


def test_container_host_wraps_argv_and_stops_being_local():
    """`is_local` is what every probe branches on to decide "act directly":
    read a file, signal a pid. None of that is true across a container
    boundary, so a container host is never local -- and over ssh the two
    wrappers nest, docker inside the remote shell."""
    local = Host().in_container("acme-odoo-1")
    assert local.is_local is False
    assert local._argv(["ps", "-eo", "pid"]) == ["docker", "exec", "acme-odoo-1", "ps", "-eo", "pid"]

    remote = Host(alias="server").in_container("acme-odoo-1")
    assert remote._argv(["kill", "-3", "1"])[:3] == ["ssh", "-o", "BatchMode=yes"]
    assert remote._argv(["kill", "-3", "1"])[-1] == "docker exec acme-odoo-1 kill -3 1"

    assert local.on_box.container is None
    assert Host().in_container(None) == Host()  # nothing to narrow, nothing changes


def test_container_shell_invocation_is_interactive():
    """`_argv`'s exec is for probes (no tty); the command a user pastes is
    the opposite -- it has to land them in a shell."""
    cmd = Host().in_container("acme-odoo-1").shell_invocation("odoo shell --no-http")
    assert cmd == "docker exec -it acme-odoo-1 sh -lc 'odoo shell --no-http'"


def test_pg_target_carries_a_containers_connection_settings():
    """A port is enough for a cluster on this box; a container's postgres
    needs an address and credentials too, and they ride as libpq env vars so
    the same argv works over ssh."""
    assert probes.PgTarget.of("5434").env_prefix == ["env", "PGPORT=5434"]
    assert probes.PgTarget.of(None).env_prefix == []
    assert probes.PgTarget.of(probes.PgTarget(port="5432")) == probes.PgTarget(port="5432")

    target = probes.PgTarget(host="172.20.0.3", port="5432", user="odoo", password="s3cret")  # noqa: S106 -- fixture
    assert target.psql("-d", "devel") == [
        "env",
        "PGHOST=172.20.0.3",
        "PGPORT=5432",
        "PGUSER=odoo",
        "PGPASSWORD=s3cret",
        "psql",
        "-w",
        "-d",
        "devel",
    ]


def test_client_port_matching_extracts_the_port_from_pg_target(monkeypatch):
    """Docker passes a full PgTarget through the job-runner probes, while
    ss reports the peer port as text. They must still match without falling
    through to lsof, which may not be installed in the container."""
    ss = 'ESTAB 0 0 172.20.0.4:45678 172.20.0.3:5433 users:(("python3",pid=42,fd=7))\n'
    monkeypatch.setattr(
        Host,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=ss, stderr=""),
    )
    monkeypatch.setattr(
        probes,
        "odoo_pid_for_port",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected lsof fallback")),
    )

    target = probes.PgTarget(host="172.20.0.3", port="5433", user="odoo")
    assert probes._pids_by_client_port(["45678"], target, Host()) == {"45678": "42"}


def test_pg_target_of_a_container_reads_the_config_odoo_itself_connects_with(monkeypatch):
    """The TUI reaches the database the same way the instance does: the
    role and password out of the container's own odoo.conf, the address off
    the compose network (postgres is normally not published to the box)."""
    monkeypatch.setattr(probes, "_container_ip", lambda *_a, **_k: "172.20.0.3")
    monkeypatch.setattr(
        probes,
        "instance_config",
        lambda *_: (Path("/opt/odoo"), _parser({"db_port": "5432", "db_user": "odoo", "db_password": "odoopassword"})),
    )

    assert probes.pg_target_of(_DOCKER_INSTANCE, Host()) == probes.PgTarget(
        host="172.20.0.3",
        port="5432",
        user="odoo",
        password="odoopassword",  # noqa: S106 -- fixture, not a real credential
    )


def test_container_databases_ignore_the_boxs_db_role_convention(monkeypatch):
    """ODOO_ACTIVITY_DB_ROLE describes *this box's* cluster (locally every
    db is owned by `openerp`). A container's cluster is its own, and the
    role odoo connects as is in its config -- so the override deliberately
    doesn't reach here, or a container would be listed with the box's
    databases."""
    monkeypatch.setattr(probes, "DB_ROLE", "openerp")
    monkeypatch.setattr(
        probes, "_container_pg_target", lambda *_: probes.PgTarget(host="172.20.0.3", port="5432", user="odoo")
    )
    asked: list[str] = []
    monkeypatch.setattr(probes, "databases_by_role", lambda role, *_a, **_k: asked.append(role) or ["devel", "e2e"])

    assert probes.databases_of(_DOCKER_INSTANCE, Host()) == (["devel", "e2e"], "5432")
    assert asked == ["odoo"]


def test_container_config_is_found_with_docker_cp_so_a_stopped_one_still_has_it(monkeypatch):
    """There is no `<workdir>/config/` convention inside an image: doodba
    renders one path, the official image ships another, and anything else
    names it on the command line.

    Probed with `docker cp`, not `docker exec`: exec needs the container
    running, and a config is exactly what you want to read when an instance
    won't start."""
    read: list[str] = []

    def fake_read(_container, path, *_a, **_k):
        read.append(str(path))
        return "[options]\ndb_user = odoo\n" if str(path) == "/opt/odoo/auto/odoo.conf" else None

    monkeypatch.setattr(probes, "_read_out_of_container", fake_read)
    monkeypatch.setattr(Host, "is_file", lambda self, path: True)  # exec would answer, and is not what's used

    at = Host().in_container("acme-odoo-1")
    assert probes.configfile_of(_DOCKER_INSTANCE, at) == Path("/opt/odoo/auto/odoo.conf")

    # one copy out of the container, not a probe followed by a read: doing
    # both separately cost four copies per databases_of
    assert read == ["/opt/odoo/auto/odoo.conf"]

    monkeypatch.setattr(probes, "_read_out_of_container", lambda *_a, **_k: None)
    assert probes.configfile_of(_DOCKER_INSTANCE, at) is None


def test_container_logs_come_from_docker_not_a_file(monkeypatch):
    """Odoo in a container writes to stdout and docker keeps the stream, so
    `logfile_of` returning None can't mean "no log" for this manager."""
    calls = _recorder(monkeypatch, stdout="hello\r\n")

    assert probes.log_snapshot(_DOCKER_INSTANCE, Host(), lines=5) == "hello\n"
    assert calls == [["docker", "logs", "--tail", "5", "acme-odoo-1"]]


def test_container_log_lines_lose_the_tty_carriage_returns():
    """docker allocates a pty when the service asks for one (doodba does),
    which turns every newline into CRLF -- and a trailing CR stops
    `_DUMP_HEADER_RE` matching, so a stack dump parses as zero workers."""
    header = "2026-08-21 04:26:08,140 1 INFO devel odoo.tools.misc: \r\n"
    assert not probes._DUMP_HEADER_RE.search(header)
    assert probes._DUMP_HEADER_RE.search(probes._untty(header))


def test_container_action_prefers_invoke_then_falls_back_to_compose(monkeypatch):
    """doodba ships a tasks.py with start/stop/restart and that's what a
    developer drives the project with, so it's what we drive too -- plain
    compose when the project has no tasks (or the box has no invoke)."""
    calls = _recorder(monkeypatch)
    monkeypatch.setattr(Host, "is_file", lambda self, path: str(path) == "/srv/acme/tasks.py")

    assert managers.instance_action(_DOCKER_INSTANCE, "restart", Host()) == ""
    assert calls == [["invoke", "-r", "/srv/acme", "restart"]]

    calls.clear()
    monkeypatch.setattr(Host, "is_file", lambda self, path: False)
    assert managers.instance_action(_DOCKER_INSTANCE, "start", Host()) == ""
    assert calls == [["docker", "compose", "--project-directory", "/srv/acme", "start"]]


def test_container_stop_never_goes_through_invoke(monkeypatch):
    """doodba's `invoke stop` is `docker compose down --remove-orphans`: it
    deletes the containers instead of stopping them, so the instance would
    disappear from the list rather than read `stopped` -- and nothing would
    be left to press `s` on. Compose's own stop leaves it exited and
    listed."""
    calls = _recorder(monkeypatch)
    monkeypatch.setattr(Host, "is_file", lambda self, path: True)  # tasks.py is right there

    assert managers.instance_action(_DOCKER_INSTANCE, "stop", Host()) == ""
    assert calls == [["docker", "compose", "--project-directory", "/srv/acme", "stop"]]


def test_container_action_falls_through_to_compose_when_invoke_fails(monkeypatch):
    """A tasks.py that doesn't define the task (or an invoke that isn't
    installed) must not leave the instance unstartable -- compose can do it
    either way. The error only surfaces if both refuse."""
    monkeypatch.setattr(Host, "is_file", lambda self, path: True)
    calls: list[list[str]] = []

    def fake_run(self, argv, input_text=None):
        calls.append(argv)
        if argv[0] == "invoke":
            return SimpleNamespace(returncode=1, stdout="", stderr="No idea what 'start' is!")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(Host, "run", fake_run)

    assert managers.instance_action(_DOCKER_INSTANCE, "start", Host()) == ""
    assert [c[0] for c in calls] == ["invoke", "docker"]


def test_container_action_without_a_project_directory_says_so(monkeypatch):
    """compose acts on a directory, not on a name: a row that lost its
    project label has nothing to act on, and saying so beats a confusing
    `docker compose` error from the wrong cwd."""
    _recorder(monkeypatch)
    assert "nothing to act on" in managers.instance_action({**_DOCKER_INSTANCE, "workdir": ""}, "restart", Host())


def test_a_stopped_project_never_falls_back_to_the_boxs_own_cluster(monkeypatch):
    """No container address means the project is down -- and an unset
    PGHOST is not "no database", it is *this box's* cluster. A stale db row
    would then report the host's databases as the container's, the exact
    mix-up the old ODOO_ACTIVITY_DOCKER hatch produced (measured: 17 of
    them). An address that cannot resolve fails on the spot instead."""
    monkeypatch.setattr(probes, "_container_ip", lambda *_a, **_k: None)
    monkeypatch.setattr(
        probes, "instance_config", lambda *_: (Path("/opt/odoo"), _parser({"db_port": "5432", "db_user": "odoo"}))
    )
    asked: list[object] = []
    monkeypatch.setattr(probes, "databases_by_role", lambda *a, **k: asked.append(a) or ["should-not-happen"])

    target = probes.pg_target_of(_DOCKER_INSTANCE, Host())
    assert target.host == "acme-db-1.invalid"
    assert "PGHOST=acme-db-1.invalid" in target.env_prefix

    assert probes.databases_of(_DOCKER_INSTANCE, Host()) == ([], None)
    assert asked == []


def test_a_stopped_containers_address_is_not_dockers_placeholder_text(monkeypatch):
    """`docker inspect` on a stopped container fills the address field with
    the literal words `invalid IP` (docker 28). Taken as a hostname it goes
    to libpq and fails to resolve, which reads as a network problem instead
    of "the container isn't running" -- so the address is parsed, not just
    picked as the first non-empty word."""
    monkeypatch.setattr(Host, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="invalid IP \n", stderr=""))
    assert probes._container_ip("acme-db-1", Host()) is None

    monkeypatch.setattr(
        Host, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=" 172.20.0.3 \n", stderr="")
    )
    assert probes._container_ip("acme-db-1", Host()) == "172.20.0.3"


def test_the_clusters_own_maintenance_database_is_not_an_instances(monkeypatch):
    """`postgres` is the cluster's maintenance database. It only started
    showing up with docker: postgres-autoconf makes POSTGRES_USER (odoo) the
    bootstrap superuser, so that role owns `postgres` too -- on a host
    cluster it belongs to the `postgres` role, which no instance matches."""
    sent: list[str] = []

    def fake_run(self, argv, input_text=None):
        sent.append(input_text or "")
        return SimpleNamespace(returncode=0, stdout="devel\ne2e\n", stderr="")

    monkeypatch.setattr(Host, "run", fake_run)

    assert probes.databases_by_role("odoo", probes.PgTarget(host="172.20.0.3"), Host()) == ["devel", "e2e"]
    assert "d.datname <> 'postgres'" in sent[0]
    assert "NOT d.datistemplate" in sent[0]


def test_pg_target_of_falls_back_to_the_containers_env_when_the_config_is_silent(monkeypatch):
    """The official odoo image ships a config with the db settings commented
    out and reads them from the environment instead, turning them into CLI
    flags its entrypoint never writes down. Without this fallback its
    database tabs come up empty (no password -> postgres refuses the
    connection), where doodba's rendered config answers directly."""
    monkeypatch.setattr(probes, "_container_ip", lambda *_a, **_k: "172.20.0.3")
    monkeypatch.setattr(probes, "instance_config", lambda *_: (Path("/etc/odoo"), _parser({})))
    inspected: list[str] = []

    def fake_run(_self, cmd, **_kw):
        inspected.append(cmd[-1])
        env = ["ODOO_RC=/etc/odoo/odoo.conf", "HOST=db", "USER=odoo", "PASSWORD=myodoo", "PORT=5432"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(env), stderr="")

    monkeypatch.setattr(Host, "run", fake_run)

    assert probes.pg_target_of(_DOCKER_INSTANCE, Host()) == probes.PgTarget(
        host="172.20.0.3",
        port="5432",
        user="odoo",
        password="myodoo",  # noqa: S106 -- fixture, not a real credential
    )
    assert inspected == ["acme-odoo-1"]  # the odoo container's env, not the db container's


def test_pg_target_of_falls_back_to_the_db_containers_own_credentials(monkeypatch):
    """A compose file that names the credentials on the db service alone
    (the odoo one then runs on the image's defaults) is the shape that
    leaves both the config and the odoo container's env silent -- so the
    role and password are read where they were actually set."""
    monkeypatch.setattr(probes, "_container_ip", lambda *_a, **_k: "172.20.0.3")
    monkeypatch.setattr(probes, "instance_config", lambda *_: (Path("/etc/odoo"), _parser({})))
    env = {
        "acme-odoo-1": ["HOST=db", "ODOO_RC=/etc/odoo/odoo.conf"],
        "acme-db-1": ["POSTGRES_DB=postgres", "POSTGRES_USER=odoo", "POSTGRES_PASSWORD=odoo"],
    }
    monkeypatch.setattr(
        Host,
        "run",
        lambda _self, cmd, **_kw: SimpleNamespace(returncode=0, stdout=json.dumps(env[cmd[-1]]), stderr=""),
    )

    assert probes.pg_target_of(_DOCKER_INSTANCE, Host()) == probes.PgTarget(
        host="172.20.0.3",
        user="odoo",
        password="odoo",  # noqa: S106 -- fixture, not a real credential
    )


_NO_QUEUE_JOB_STDERR = (
    'psql:<stdin>:1: ERROR:  relation "queue_job" does not exist\n'
    "LINE 1: ...max(age((now() at time zone 'utc'), date_created)) FROM queue_...\n"
    "                                                                    ^\n"
)


def test_jobs_says_the_module_is_not_installed_instead_of_quoting_postgres(monkeypatch):
    """Most databases never install queue_job, so the missing table is the
    Jobs tab's normal empty case, not a fault -- but postgres phrases it as
    an ERROR with a LINE/caret excerpt, which reads like something broke."""
    monkeypatch.setattr(
        Host,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr=_NO_QUEUE_JOB_STDERR),
    )

    assert probes.job_groups("demo", None, Host()) == (None, "(queue_job is not installed on this database)")
    assert probes.jobs_in_group("demo", "f", "started", None, Host()) == (
        None,
        "(queue_job is not installed on this database)",
    )
    assert probes.requeue_jobs("demo", None, Host()) == (0, "(queue_job is not installed on this database)")


def test_any_other_jobs_error_is_still_quoted_verbatim(monkeypatch):
    """Only the missing-module case is rephrased: every other failure is
    rarer, and its exact text is the diagnosis."""
    monkeypatch.setattr(
        Host,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stdout="", stderr="psql: error: connection to server failed: FATAL:  too many clients\n"
        ),
    )

    _rows, error = probes.job_groups("demo", None, Host())
    assert "too many clients" in error


def test_a_containers_workdir_comes_off_the_image_not_a_running_process(monkeypatch):
    """/proc/<pid>/cwd would cost a ps plus a readlink and answers nothing
    at all for a stopped container -- which still has paths worth
    resolving (its config, see the test above)."""
    calls: list[list[str]] = []

    def fake_run(self, argv, input_text=None):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="/opt/odoo\n", stderr="")

    monkeypatch.setattr(Host, "run", fake_run)

    assert probes.instance_workdir(_DOCKER_INSTANCE, Host()) == Path("/opt/odoo")
    assert calls == [["docker", "inspect", "-f", "{{.Config.WorkingDir}}", "acme-odoo-1"]]


def test_neutralized_databases_reads_the_cluster_in_one_odoo_db_run(monkeypatch):
    """The row tag is a binary claim off `odoo-db list`, one process for the
    whole cluster -- it runs on every instance highlight, sometimes against
    a host already loaded enough to be worth debugging.

    A db odoo-db skipped (not an Odoo database, or it would not open) is
    absent from the map: unknown, which the UI shows as no tag rather than
    as a guess in either direction."""
    seen: list[list[str]] = []

    def popen(_self, argv, **_kw):
        seen.append(argv)
        payload = json.dumps([
            {"db": "staging", "version": "19.0", "neutralized": True},
            {"db": "prod", "version": "17.0", "neutralized": False},
        ])
        return SimpleNamespace(communicate=lambda timeout=None: (payload, ""), kill=lambda: None)

    monkeypatch.setattr(Host, "popen", popen)

    assert probes.neutralized_databases("5433", Host()) == {"staging": True, "prod": False}
    # `list` is cluster-wide: no database argument, one run
    assert seen == [["env", "PGPORT=5433", "odoo-db", "--output-format", "json", "list"]]


def test_neutralized_databases_answers_nothing_when_odoo_db_cannot(monkeypatch):
    """No odoo-db on the host, or a run that hangs, leaves every row
    untagged rather than tagging them all as live -- a wrong green and a
    wrong red are both worse than no tag."""
    monkeypatch.setattr(Host, "popen", lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError))
    assert probes.neutralized_databases(None, Host()) == {}

    killed: list[bool] = []

    def hanging(_self, _argv, **_kw):
        def communicate(timeout=60.0):
            raise subprocess.TimeoutExpired("odoo-db", timeout)

        return SimpleNamespace(communicate=communicate, kill=lambda: killed.append(True))

    monkeypatch.setattr(Host, "popen", hanging)
    assert probes.neutralized_databases(None, Host()) == {}
    assert killed == [True]  # not left running behind us
