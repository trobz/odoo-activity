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
    monkeypatch.setattr(probes, "_exe_of", lambda *_: None)
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
    monkeypatch.setattr(probes, "_exe_of", lambda *_: None)
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
    """A bare `python3` argv[0] gets upgraded to `_exe_of`'s resolved path."""
    monkeypatch.setattr(probes, "procs_of", _fake_procs("python3 /opt/odoo/odoo-bin -c /etc/odoo.conf"))
    monkeypatch.setattr(probes, "_exe_of", lambda pid, host: "/venv/bin/python3" if pid == "1" else None)

    cmd = probes.shell_command(_INSTANCE, Host())
    assert cmd == "/venv/bin/python3 /opt/odoo/odoo-bin shell --no-http -c /etc/odoo.conf"
