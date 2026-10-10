import subprocess
from pathlib import Path

from odoo_activity.host import _REMOTE_PATH_FIX, _SSH_OPTS, Host


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

    assert captured["cmd"] == ["ssh", *_SSH_OPTS, "openerp@demo", f"{_REMOTE_PATH_FIX} echo 'hi there'"]


def test_config_file_adds_dash_capital_f(monkeypatch):
    """A Host carrying a config_file resolves its alias through that file:
    oa-mcp-multi's --host-file must be authoritative for connecting, not just
    for listing, or ssh silently falls back to ~/.ssh/config."""
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    Host(alias="demo", port=2222, config_file=Path("/etc/oa/hosts")).run(["echo", "hi"])

    assert captured["cmd"] == [
        "ssh",
        *_SSH_OPTS,
        "-F",
        "/etc/oa/hosts",
        "-p",
        "2222",
        "demo",
        f"{_REMOTE_PATH_FIX} echo hi",
    ]


def test_no_config_file_means_no_dash_f(monkeypatch):
    """Unset config_file leaves ssh on its own default -- no -F, so a server
    started without --host-file never points ssh at a possibly-absent file."""
    captured = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    Host(alias="demo").run(["echo", "hi"])

    assert "-F" not in captured["cmd"]


def test_remote_popen_wraps_in_ssh(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return "the-child"

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    child = Host(alias="x").popen(["tail", "-f", "/var/log/odoo.log"])

    assert captured["cmd"] == ["ssh", *_SSH_OPTS, "x", f"{_REMOTE_PATH_FIX} tail -f /var/log/odoo.log"]
    assert child == "the-child"


def test_remote_path_fix_puts_the_expanded_local_bin_first():
    """odoo.sh bakes `~/.local/bin` into PATH; sshd's dash won't expand `~`
    there the way bash does, hiding our tools. Prepending the expanded dir
    fixes the lookup without touching the entries we were handed.

    Runs through a real shell: the tests above only prove the fix gets
    spliced in, not that it does anything.
    """

    def path_under_sh(path: str) -> str:
        # nested `sh -c`: a prefix assignment reaches the command's own
        # environment, but the outer shell expands `$PATH` before it applies
        run = subprocess.run(  # noqa: S603  -- our own constant, not untrusted input
            ["sh", "-c", f"""{_REMOTE_PATH_FIX} sh -c 'printf %s "$PATH"'"""],  # noqa: S607  -- `sh` is the point
            env={"PATH": path, "HOME": "/home/odoo"},
            capture_output=True,
            text=True,
        )
        return run.stdout

    # odoo.sh's own PATH: the unusable `~/.local/bin` stays, but the usable
    # one now comes first, so the lookup finds our tools
    assert path_under_sh("~/.local/bin:/usr/bin") == "/home/odoo/.local/bin:~/.local/bin:/usr/bin"
    # everywhere else: inherited entries keep their order, one dir ahead of them
    assert path_under_sh("/usr/bin:/bin") == "/home/odoo/.local/bin:/usr/bin:/bin"


def test_a_missing_tool_is_exit_127_not_an_exception():
    """A box without `psql` (or `systemctl`, or `docker`) must degrade the
    one probe that needed it, not raise: an exception propagates out of
    whichever worker made the call and takes the whole fetch down with it --
    on the instances list, that means losing every database row rather than
    one status. 127 is what a shell reports for the same thing, and it is
    already what the remote branch produces, where ssh runs and the missing
    tool is the far box's problem.
    """
    result = Host().run(["__no_such_tool__", "--version"])

    assert result.returncode == 127
    assert result.stdout == ""
    assert "command not found" in result.stderr

    # and with stdin, the other branch of the same call
    assert Host().run(["__no_such_tool__"], input_text="x").returncode == 127
