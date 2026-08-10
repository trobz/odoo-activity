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

    found = probes.local_instances(Host())

    assert [(inst["name"], inst["pid"]) for inst in found] == [
        ("v18c_queue [100]", "100"),
        ("v18c_queue [102]", "102"),
    ]
    assert found[0]["config"] == "/home/x/code/oca/odoo.conf"  # `./odoo.conf` resolved against cwd
    assert found[0]["uptime"] == "0:01:00"


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
