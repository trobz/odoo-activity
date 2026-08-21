"""Which processes are Odoo instances, and what each one is called.

One suite for all four managers — where an instance is deployed (a systemd
unit, supervisor, odoo.sh, a bare process) only changes what gets asked;
telling an instance apart from a lookalike and naming it is the same job.
The argv strings below are trimmed from real processes on a dev machine.
"""

from types import SimpleNamespace

from odoo_activity import probes
from odoo_activity.host import Host
from odoo_activity.probes import Instance, ProcRow

_EGG = "/home/x/venvs/venv-odoo18/bin/python /home/x/venvs/venv-odoo18/bin/odoo --config ./odoo.conf -d v18c_queue"
_BIN = "python3 /home/x/demo/18.0/odoo/odoo-bin --config config/local.conf -d demo"
_SUPERVISED = "/home/x/venvs/demo/bin/python odoo/odoo-bin --config config/supervisor.conf -d prod"
_WRAPPER = "/usr/bin/python3 /home/x/.local/bin/pew in venv-odoo18 /home/x/venvs/venv-odoo18/bin/odoo -d v18c_queue"

_SCOPE_CGROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-ab-cd.scope\n"
_UNIT_CGROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/odoo-demo.service\n"


def _row(pid: str, ppid: str, cmd: str) -> ProcRow:
    return {"pid": pid, "ppid": ppid, "user": "x", "mem": "1.0", "nice": "0", "cmd": cmd}


def test_systemd_instances_filters_templates_and_maps_status(monkeypatch):
    files = (
        "gnome@.service           disabled enabled\n"  # template, dropped
        "wiki.service             enabled  enabled\n"  # not odoo, filtered
        "openerp-demo.service    disabled enabled\n"  # matched by name only
        "odoo-demo.service        disabled enabled\n"
        "odoo-crashed.service     disabled enabled\n"
    )
    show = (
        "Id=wiki.service\nDescription=A wiki\nActiveState=inactive\n\n"
        "Id=openerp-demo.service\nDescription=Staging\nActiveState=inactive\n\n"
        "Id=odoo-demo.service\nDescription=Odoo odoo 18.0 instance\n"
        "ActiveState=active\nActiveEnterTimestampMonotonic=1000000\n\n"
        "Id=odoo-crashed.service\nDescription=Odoo crashed instance\nActiveState=failed\n"
    )

    def fake_run(cmd, **_):
        return SimpleNamespace(stdout=files if "list-unit-files" in cmd else show)

    monkeypatch.setattr(probes.subprocess, "run", fake_run)
    monkeypatch.setattr(probes.time, "clock_gettime", lambda _clk: 61.0)  # 60s after entering active

    assert probes.systemd_instances() == [
        {"name": "openerp-demo.service", "status": "stopped", "uptime": "-", "manager": "systemd"},
        {"name": "odoo-demo.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"},
        {"name": "odoo-crashed.service", "status": "failed", "uptime": "-", "manager": "systemd"},
    ]


def test_supervisor_instances_maps_status_vocab_and_uptime(monkeypatch, tmp_path):
    monkeypatch.setattr(probes, "SUPERVISOR_CONFD", tmp_path / "absent")  # status only
    status = (
        "/usr/bin/supervisorctl:6: DeprecationWarning: pkg_resources is deprecated\n"
        "  from pkg_resources import load_entry_point\n"
        "mailhog                        RUNNING   pid 23107, uptime 9:19:07\n"  # not odoo, filtered
        "openerp-odoo-staging           RUNNING   pid 19841, uptime 0:05:00\n"
        "openerp-odoo-crashed           FATAL     Exited too quickly\n"
        "openerp-odoo-exited            EXITED    Jul 02 10:59 AM\n"
        "openerp-odoo18-staging         STOPPED   Not started\n"
    )
    monkeypatch.setattr(probes.subprocess, "run", lambda *_, **__: SimpleNamespace(stdout=status))
    names = [(inst["name"], inst["status"], inst["uptime"]) for inst in probes.supervisor_instances()]

    assert names == [
        ("openerp-odoo-crashed", "fatal", "-"),
        ("openerp-odoo-exited", "exited", "-"),
        ("openerp-odoo-staging", "running", "0:05:00"),
        ("openerp-odoo18-staging", "stopped", "-"),
    ]


def test_detects_any_runner_not_just_odoo_bin():
    """An egg-installed console script is an instance; a file merely *named*
    odoo, or a db client connecting as the odoo user, is not — `_is_odoo`
    alone matches all of them, the entry-point gate is what separates them."""
    assert probes._looks_like_odoo(_EGG) is True
    assert probes._looks_like_odoo(_BIN) is True
    assert probes._looks_like_odoo("odoo-bin") is True

    assert probes._looks_like_odoo("vi odoo/odoo/tools/config.py") is False
    assert probes._looks_like_odoo("postgres: 16/main: openerp demo ::1(42678) idle") is False
    assert probes._looks_like_odoo("/usr/lib/postgresql/16/bin/psql -U odoo postgres") is False
    assert probes._looks_like_odoo("docker exec -it pg-16 bash -c psql -U odoo -d odoo_demo") is False
    assert probes._looks_like_odoo("sshd-session: odoo [priv]") is False
    assert probes._looks_like_odoo("sshd: odoo@pts/0") is False


def test_skips_roots_owned_by_a_manager(monkeypatch):
    """Only a root owned by a shell is ours — systemd's and supervisord's are
    already listed by those managers, a containerized one waits on its own
    manager, and a prefork worker is no root at all. Two shells serving the
    same db are two instances, told apart by their master pid."""
    by_pid = {
        "100": _row("100", "10", _EGG),
        "101": _row("101", "100", _EGG),  # prefork worker
        "102": _row("102", "10", _EGG),  # a second runner on the same db
        "200": _row("200", "20", _BIN),
        "300": _row("300", "30", _SUPERVISED),
        "400": _row("400", "40", _BIN),
        "10": _row("10", "1", "-zsh"),
        "20": _row("20", "1", "/lib/systemd/systemd --user"),
        "30": _row("30", "1", "/usr/bin/python3 /usr/bin/supervisord -n"),
        "40": _row("40", "4", "/bin/sh /entrypoint.sh"),  # container init, one hop below the shim
        "4": _row("4", "1", "/usr/bin/containerd-shim-runc-v2 -namespace moby -id abc"),
    }
    monkeypatch.setattr(probes, "_ps_snapshot", lambda *_: (by_pid, {}))
    monkeypatch.setattr(probes, "_proc_link", lambda pid, name, host: "/home/x/code/oca" if name == "cwd" else None)
    monkeypatch.setattr(probes, "_proc_uptime", lambda *_: 60.0)
    # 200 is systemd's, so its cgroup has to say so: these pids are made up,
    # and whatever really holds them on the machine running the suite would
    # otherwise answer for them
    monkeypatch.setattr(Host, "read_text", lambda *_: _UNIT_CGROUP)

    found = probes.local_instances(Host())

    assert [(inst["name"], inst["pid"]) for inst in found] == [
        ("v18c_queue [100]", "100"),
        ("v18c_queue [102]", "102"),
    ]
    assert found[0]["config"] == "/home/x/code/oca/odoo.conf"  # `./odoo.conf` resolved against cwd
    assert found[0]["uptime"] == "0:01:00"


def test_reparented_runner_is_told_apart_from_a_real_unit(monkeypatch):
    """`systemd --user` reaps orphans, so a directly-run instance lands under
    it as soon as its wrapper or shell exits. Dropping every root it parents
    lost that instance for good — `systemd_instances` has no unit to list it
    under either. The cgroup, which reparenting doesn't move, is what tells
    the orphan (a terminal's scope) from a unit systemd really does run."""
    by_pid = {
        "100": _row("100", "20", _EGG),  # orphaned runner, reaped by systemd --user
        "200": _row("200", "20", _BIN),  # a genuine odoo-demo.service
        "20": _row("20", "1", "/lib/systemd/systemd --user"),
    }
    cgroups = {"/proc/100/cgroup": _SCOPE_CGROUP, "/proc/200/cgroup": _UNIT_CGROUP}
    monkeypatch.setattr(probes, "_ps_snapshot", lambda *_: (by_pid, {}))
    monkeypatch.setattr(probes, "_proc_link", lambda *_: None)
    monkeypatch.setattr(probes, "_proc_uptime", lambda *_: 60.0)
    monkeypatch.setattr(Host, "read_text", lambda _self, path: cgroups[str(path)])

    assert [(inst["name"], inst["pid"]) for inst in probes.local_instances(Host())] == [("v18c_queue", "100")]


def test_unreadable_cgroup_keeps_the_parent_based_answer(monkeypatch):
    """A root whose cgroup can't be read is "don't know", not "not a unit":
    it stays skipped, since listing a real unit twice is the worse failure."""
    by_pid = {"100": _row("100", "20", _EGG), "20": _row("20", "1", "/lib/systemd/systemd --user")}
    monkeypatch.setattr(probes, "_ps_snapshot", lambda *_: (by_pid, {}))
    monkeypatch.setattr(Host, "read_text", lambda *_: "")  # what a remote `cat` failure looks like

    assert probes.local_instances(Host()) == []


def test_row_points_at_odoo_itself_not_the_wrapper_that_spawned_it(monkeypatch):
    """The pid on the row is what gets signalled (`K`, `L`, `D`). A wrapper
    like `pew` has no SIGQUIT handler, so dumping stacks through it killed it
    — and the shell it was started from with it — instead of dumping."""
    by_pid = {
        "99": _row("99", "10", _WRAPPER),
        "100": _row("100", "99", _EGG),  # the odoo master the wrapper exec'd
        "101": _row("101", "100", _EGG),  # prefork worker
        "10": _row("10", "1", "-zsh"),
    }
    children = {"99": ["100"], "100": ["101"]}
    monkeypatch.setattr(probes, "_ps_snapshot", lambda *_: (by_pid, children))
    monkeypatch.setattr(probes, "_proc_link", lambda *_: None)
    monkeypatch.setattr(probes, "_proc_uptime", lambda *_: 60.0)

    found = probes.local_instances(Host())

    assert [(inst["name"], inst["pid"]) for inst in found] == [("v18c_queue", "100")]
    assert found[0]["command"] == _EGG  # the master's own argv, not the wrapper's


def test_odoo_master_stops_at_the_first_real_odoo_process():
    assert probes._runs_odoo_itself(_EGG) is True
    assert probes._runs_odoo_itself(_BIN) is True
    assert probes._runs_odoo_itself(_WRAPPER) is False  # names odoo, but runs pew

    by_pid = {"99": _row("99", "10", _WRAPPER), "100": _row("100", "99", _EGG)}
    assert probes._odoo_master("99", by_pid, {"99": ["100"]}) == "100"
    assert probes._odoo_master("100", by_pid, {}) == "100"
    # a wrapper with no odoo child left to step into is the best answer there is
    assert probes._odoo_master("99", by_pid, {}) == "99"


def test_odoo_master_does_not_descend_into_a_lone_worker():
    """A master run through a custom launcher isn't in `_ODOO_ENTRYPOINTS`,
    though `_looks_like_odoo` still matches it on `--config`. Descending on
    child count alone then walked past it into its one worker, handing that
    worker every field on the row and every signal `K`/`L`/`D` sends. A fork
    shares its parent's entry point; a wrapper's exec does not."""
    launcher = "/venv/bin/python /opt/odoo/server.py --config /etc/odoo.conf"
    by_pid = {
        "100": _row("100", "10", launcher),
        "101": _row("101", "100", f"{launcher} gevent"),  # its only child
    }

    assert probes._runs_odoo_itself(launcher) is False
    assert probes._looks_like_odoo(launcher) is True
    assert probes._odoo_master("100", by_pid, {"100": ["101"]}) == "100"


def test_only_systemds_own_cgroup_hierarchy_names_the_unit():
    """cgroup v1 lists one line per controller, and the controllers systemd
    doesn't drive commonly stop at the user manager — `…/user@1000.service`,
    a leaf that reads as a unit but is an ancestor of terminals too. Reading
    it as one put every directly-run instance back under systemd, the exact
    disappearance this fix is about."""
    v1_terminal = (
        "12:pids:/user.slice/user-1000.slice/user@1000.service\n"
        "4:memory:/user.slice/user-1000.slice/user@1000.service\n"
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-ab-cd.scope\n"
    )
    v1_unit = (
        "12:pids:/user.slice/user-1000.slice/user@1000.service\n"
        "1:name=systemd:/user.slice/user-1000.slice/user@1000.service/odoo-demo.service\n"
    )

    assert probes._runs_under_unit(v1_terminal) is False
    assert probes._runs_under_unit(v1_unit) is True
    assert probes._runs_under_unit(_SCOPE_CGROUP) is False
    assert probes._runs_under_unit(_UNIT_CGROUP) is True
    # nothing systemd's own hierarchy answers for: "don't know", so keep the
    # parent-based answer rather than list a real unit twice
    assert probes._runs_under_unit("") is True
    assert probes._runs_under_unit("12:pids:/user.slice\n") is True


def test_named_after_project_not_version_dir(monkeypatch):
    """With no `-d` to name it, a bare version directory names its parent —
    "18.0" identifies nothing, and the name is what keeps a row the same row
    across polls."""
    by_pid = {"100": _row("100", "10", "odoo-bin --addons-path /a"), "10": _row("10", "1", "-zsh")}
    monkeypatch.setattr(probes, "_ps_snapshot", lambda *_: (by_pid, {}))
    monkeypatch.setattr(probes, "_proc_link", lambda pid, name, host: "/home/x/demo/18.0" if name == "cwd" else None)
    monkeypatch.setattr(probes, "_proc_uptime", lambda *_: 1.0)

    assert probes.local_instances(Host())[0]["name"] == "demo"


def _inst(status: str) -> Instance:
    return {"name": "x", "status": status, "uptime": "-", "manager": "systemd"}


def test_instance_status_promotes_ambiguous_stopped_but_not_explicit_failure(monkeypatch):
    # a live process promotes an ambiguous "stopped" report to running
    monkeypatch.setattr(probes, "procs_of", lambda *_: [{"pid": "1"}])
    assert probes.instance_status(_inst("stopped")) == "running"

    # regression: an explicit failure is authoritative even with a live
    # process matching the same db — procs_of() matches by db name, not
    # manager, so that process may belong to the *other* manager's instance
    assert probes.instance_status(_inst("failed")) == "failed"

    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    assert probes.instance_status(_inst("stopped")) == "stopped"


# --- docker ---------------------------------------------------------------

_DOODBA_CMD = "/opt/odoo/common/entrypoint odoo --workers=0 --dev=reload,qweb"


def _ps_line(name, state, image, command, project, service, workdir="/srv/p"):
    return "\t".join((name, state, image, command, project, service, workdir))


def _docker_host(monkeypatch, ps_output, ran=None):
    """A Host whose `docker ps` answers `ps_output`; everything else is a
    no-op success, so a test only has to describe the containers."""

    def fake_run(self, argv, input_text=None):
        if ran is not None:
            ran.append(argv)
        stdout = ps_output if argv[:2] == ["docker", "ps"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(Host, "run", fake_run)
    monkeypatch.setattr(probes, "_odoo_master_in", lambda *_: "1")
    monkeypatch.setattr(probes, "_proc_uptime", lambda *_: 90.0)
    return Host()


def test_docker_instances_are_one_row_per_compose_project(monkeypatch):
    """A compose project is the instance: odoo and its postgres are two
    halves of one, and the project name is what the developer calls it.
    Containers with no compose project are skipped -- nothing to run
    `invoke`/`docker compose` against."""
    host = _docker_host(
        monkeypatch,
        "\n".join((
            _ps_line("acme-odoo-1", "running", "acme-odoo", _DOODBA_CMD, "acme", "odoo", "/srv/acme"),
            _ps_line("acme-db-1", "running", "postgres:16", "docker-entrypoint.sh postgres", "acme", "db", "/srv/acme"),
            _ps_line("acme-smtp-1", "running", "mailhog/mailhog", "MailHog", "acme", "smtp", "/srv/acme"),
            _ps_line("loose-odoo", "running", "odoo:18", "odoo", "", ""),  # no compose project
        )),
    )

    found = probes.docker_instances(host)

    assert found == [
        {
            "name": "acme",
            "status": "running",
            "uptime": "0:01:30",
            "manager": "docker",
            "container": "acme-odoo-1",
            "command": _DOODBA_CMD,
            "workdir": "/srv/acme",
            "db_container": "acme-db-1",
        }
    ]


def test_docker_instances_finds_postgres_by_image_when_the_service_was_renamed(monkeypatch):
    """The `db` service name is a convention, the postgres image is
    evidence -- either is enough, so a renamed service still resolves."""
    host = _docker_host(
        monkeypatch,
        "\n".join((
            _ps_line("p-odoo-1", "running", "p-odoo", _DOODBA_CMD, "p", "odoo"),
            _ps_line("p-pg-1", "running", "ghcr.io/tecnativa/postgres-autoconf:16", "postgres", "p", "warehouse"),
        )),
    )

    assert probes.docker_instances(host)[0]["db_container"] == "p-pg-1"


def test_docker_instances_lists_a_stopped_project_with_no_uptime(monkeypatch):
    """A stopped instance still has to be listed, the way a stopped systemd
    unit is -- `s` starting it is the whole point. `restarting` is a crash
    loop, not a healthy start, so it reads as failed."""
    host = _docker_host(
        monkeypatch,
        "\n".join((
            _ps_line("a-odoo-1", "exited", "a-odoo", _DOODBA_CMD, "a", "odoo"),
            _ps_line("b-odoo-1", "restarting", "b-odoo", _DOODBA_CMD, "b", "odoo"),
        )),
    )

    assert [(i["name"], i["status"], i["uptime"]) for i in probes.docker_instances(host)] == [
        ("a", "stopped", "-"),
        ("b", "failed", "-"),
    ]


def test_docker_instances_names_a_second_odoo_service_apart(monkeypatch):
    """One project, two odoo services (an http one and a longpolling/cron
    one): both are listed, and `<project>/<service>` keeps them apart."""
    host = _docker_host(
        monkeypatch,
        "\n".join((
            _ps_line("m-odoo-1", "running", "m-odoo", _DOODBA_CMD, "m", "odoo"),
            _ps_line("m-cron-1", "running", "m-odoo", _DOODBA_CMD, "m", "odoo_cron"),
        )),
    )

    assert [i["name"] for i in probes.docker_instances(host)] == ["m/odoo", "m/odoo_cron"]


def test_docker_instances_ignores_a_project_with_no_odoo_in_it(monkeypatch):
    """A compose project is not an odoo instance by virtue of existing --
    the same argv test every other manager uses decides."""
    host = _docker_host(
        monkeypatch,
        _ps_line("web-nginx-1", "running", "nginx", "nginx -g daemon off;", "web", "nginx"),
    )

    assert probes.docker_instances(host) == []


def test_docker_ps_drops_the_quotes_docker_wraps_the_command_in(monkeypatch):
    """`docker ps` prints .Command quoted; left in, the quote sits in front
    of the entrypoint path and _is_odoo_process never sees a program name."""
    host = _docker_host(monkeypatch, _ps_line("q-odoo-1", "running", "q", f'"{_DOODBA_CMD}"', "q", "odoo"))

    assert probes._docker_ps(host)[0]["command"] == _DOODBA_CMD
    assert probes.docker_instances(host)[0]["name"] == "q"


def test_docker_instances_are_empty_without_docker(monkeypatch):
    """No docker on the box: degrade to "no instances", the same way a
    missing supervisorctl does -- not a crash."""

    def no_docker(self, argv, input_text=None):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(Host, "run", no_docker)
    assert probes.docker_instances(Host()) == []
