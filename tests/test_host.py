import subprocess

from odoo_activity.host import _SSH_OPTS, Host


def test_spawns_never_inherit_our_stdin(monkeypatch):
    """Inherited stdin is the terminal Textual reads keystrokes from, and ssh
    forwards stdin to the remote command — every probe then races the input
    thread and eats some of the user's typing (see _NO_STDIN)."""
    calls = []

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(kw) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(kw))

    Host(alias="h").run(["echo", "hi"])
    Host(alias="h").popen(["tail", "-f", "/x"])
    Host().run(["echo", "hi"])

    assert [kw.get("stdin") for kw in calls] == [subprocess.DEVNULL] * 3

    # the psql path feeds SQL in, so it pipes stdin rather than closing it —
    # still never the terminal
    calls.clear()
    Host(alias="h").run(["psql"], input_text="select 1")

    assert "stdin" not in calls[0] and calls[0]["input"] == "select 1"


def test_is_local():
    assert Host().is_local
    assert not Host(alias="x").is_local


def test_local_run_execs_argv_directly(monkeypatch):
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = Host().run(["echo", "hi"])

    assert captured["cmd"] == ["echo", "hi"]
    assert result.stdout == "ok"


def test_remote_run_wraps_in_ssh(monkeypatch):
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    Host(alias="openerp@demo").run(["echo", "hi there"])

    assert captured["cmd"] == ["ssh", *_SSH_OPTS, "openerp@demo", "echo 'hi there'"]


def test_remote_popen_wraps_in_ssh(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return "the-child"

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    child = Host(alias="x").popen(["tail", "-f", "/var/log/odoo.log"])

    assert captured["cmd"] == ["ssh", *_SSH_OPTS, "x", "tail -f /var/log/odoo.log"]
    assert child == "the-child"
