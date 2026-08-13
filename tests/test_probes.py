import subprocess
from pathlib import Path

import pyperclip

from odoo_activity import probes
from odoo_activity.host import Host
from odoo_activity.probes import Instance

_INSTANCE: Instance = {"name": "demo", "status": "running", "uptime": "0:01:00", "manager": "systemd"}


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
