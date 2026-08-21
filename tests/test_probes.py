import configparser
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pyperclip

from odoo_activity import probes
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
    monkeypatch.setattr(probes, "pg_target_of", lambda *_: probes.PgTarget(host="172.20.0.3", port="5432", user="odoo"))
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
    copied: list[str] = []

    def fake_copy(_container, path, *_a, **_k):
        copied.append(str(path))
        return "copied-to-here" if str(path) == "/opt/odoo/auto/odoo.conf" else None

    monkeypatch.setattr(probes, "_copy_out_of_container", fake_copy)
    monkeypatch.setattr(Host, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(Host, "is_file", lambda self, path: True)  # exec would answer, and is not what's used

    at = Host().in_container("acme-odoo-1")
    assert probes.configfile_of(_DOCKER_INSTANCE, at) == Path("/opt/odoo/auto/odoo.conf")
    assert copied == ["/opt/odoo/auto/odoo.conf"]

    monkeypatch.setattr(probes, "_copy_out_of_container", lambda *_a, **_k: None)
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

    assert probes.instance_action("acme", "restart", "docker", Host(), "/srv/acme") == ""
    assert calls == [["invoke", "-r", "/srv/acme", "restart"]]

    calls.clear()
    monkeypatch.setattr(Host, "is_file", lambda self, path: False)
    assert probes.instance_action("acme", "start", "docker", Host(), "/srv/acme") == ""
    assert calls == [["docker", "compose", "--project-directory", "/srv/acme", "start"]]


def test_container_stop_never_goes_through_invoke(monkeypatch):
    """doodba's `invoke stop` is `docker compose down --remove-orphans`: it
    deletes the containers instead of stopping them, so the instance would
    disappear from the list rather than read `stopped` -- and nothing would
    be left to press `s` on. Compose's own stop leaves it exited and
    listed."""
    calls = _recorder(monkeypatch)
    monkeypatch.setattr(Host, "is_file", lambda self, path: True)  # tasks.py is right there

    assert probes.instance_action("acme", "stop", "docker", Host(), "/srv/acme") == ""
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

    assert probes.instance_action("acme", "start", "docker", Host(), "/srv/acme") == ""
    assert [c[0] for c in calls] == ["invoke", "docker"]


def test_container_action_without_a_project_directory_says_so(monkeypatch):
    """compose acts on a directory, not on a name: a row that lost its
    project label has nothing to act on, and saying so beats a confusing
    `docker compose` error from the wrong cwd."""
    _recorder(monkeypatch)
    assert "nothing to act on" in probes.instance_action("acme", "restart", "docker", Host(), None)


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
