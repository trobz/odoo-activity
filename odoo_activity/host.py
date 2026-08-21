"""Local-or-remote command dispatch — one Host, one code path.

Every probe goes through a Host so it can run against the box
odoo-activity is on, or a remote one over ssh, without knowing which.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob as _glob
import os
import shlex
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")

_PROBE_POOL = ThreadPoolExecutor(thread_name_prefix="oa-probe")


async def to_thread(fn: Callable[..., _T], *args: object, **kwargs: object) -> _T:
    """`asyncio.to_thread` on our own pool, not asyncio's default.

    `asyncio.run` (inside `App.run`) joins the *default* executor on the way
    out, with no timeout on 3.10 — so one in-flight ssh call at quit keeps
    `App.run` from returning and main.py's `os._exit` from ever running.
    """
    return await asyncio.get_running_loop().run_in_executor(_PROBE_POOL, partial(fn, *args, **kwargs))


# ControlMaster: the first call to a given (user, host, port) opens a real
# ssh connection and keeps it open 10 min past the last command; every
# later call reuses it instead of paying a fresh TCP+SSH handshake -- the
# difference between "smooth" and "hangs" once a poll timer fires
# per-second calls. %C is ssh's own hash of the connection's identity, so
# distinct targets never collide on one socket.
# Plus our pid: a hard-killed run leaves a dead-but-present master, and ssh
# has no timeout connecting to the socket, so every later run against that
# host hangs until `rm ~/.ssh/oa-cm-*`. Costs one extra handshake per run.
_CONTROL_PATH = str(Path.home() / ".ssh" / f"oa-cm-%C-{os.getpid()}")
_SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPersist=600",
    "-o",
    f"ControlPath={_CONTROL_PATH}",
    # ConnectTimeout only covers the initial handshake -- once a session is
    # up, a stalled network (or an unresponsive remote) can otherwise hang
    # a command forever, which asyncio.to_thread can't interrupt (see main.py
    # on why that then keeps the whole process from exiting). Give up after
    # ~10s of silence instead.
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=2",
]


# subprocess inherits our stdin, which is the terminal Textual reads keys
# from, and ssh forwards stdin to the remote command (BatchMode only stops
# prompts). Every probe was a second reader stealing the user's keystrokes.
_NO_STDIN = subprocess.DEVNULL


@dataclass(frozen=True)
class Host:
    """`alias=None` is this box; otherwise an ssh destination (`user@host`
    or a `~/.ssh/config` alias), optionally on a non-default `port`.

    `container` runs every command inside that docker container instead of
    on the box itself -- `docker exec` first, then the ssh wrapper if
    there's also an `alias`, so a container on a remote host is just both
    at once. It deliberately makes `is_local` False: everything that would
    otherwise touch the filesystem directly (`read_text`, `stat_size`,
    `glob`, ...) has to go out as argv for a container the same way it does
    for a remote box, and the pid a signal names has to be the one the
    container's own pid namespace uses.
    """

    alias: str | None = None
    port: int | None = None
    container: str | None = None

    @property
    def is_local(self) -> bool:
        """This process can act on the target directly -- same filesystem,
        same pid namespace. False for ssh *and* for a container (see the
        class docstring)."""
        return self.alias is None and self.container is None

    @property
    def on_box(self) -> Host:
        """The same target with the container stripped off -- for the few
        tools that must run on the box itself (`docker cp`, `docker logs`)
        even though the thing they act on lives in a container."""
        return self if self.container is None else replace(self, container=None)

    def in_container(self, container: str | None) -> Host:
        """The same target, but inside `container` (unchanged if None, so a
        caller can pass an instance's container name without branching)."""
        return self if container is None else replace(self, container=container)

    def _argv(self, argv: list[str]) -> list[str]:
        if self.container is not None:
            # -i is deliberate and -t deliberately absent: probes pipe
            # stdin/stdout, they never sit on a tty (see shell_invocation
            # for the interactive counterpart).
            argv = ["docker", "exec", self.container, *argv]
        if self.alias is None:
            return argv
        port_opts = ["-p", str(self.port)] if self.port else []
        return ["ssh", *_SSH_OPTS, *port_opts, self.alias, shlex.join(argv)]

    def shell_invocation(self, cmd: str) -> str:
        """`cmd` as the user should paste it into their own terminal to
        reach an interactive shell: unchanged when local, or the ssh
        command that lands them in one over an allocated pty when remote.

        Deliberately skips our own `_SSH_OPTS` -- `BatchMode=yes` is right
        for a headless probe, wrong for a session the user is about to sit
        in front of and possibly type a password into.

        A container gets `-it` here, unlike `_argv`'s exec: this one *is*
        the interactive session.
        """
        if self.container is not None:
            cmd = shlex.join(["docker", "exec", "-it", self.container, "sh", "-lc", cmd])
        if self.alias is None:
            return cmd
        port_opts = ["-p", str(self.port)] if self.port else []
        return shlex.join(["ssh", "-t", *port_opts, self.alias, cmd])

    def run(self, argv: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        """Like subprocess.run(capture_output=True, text=True), local or over ssh.

        Never inherits our stdin — see _NO_STDIN.
        """
        if input_text is None:
            return subprocess.run(self._argv(argv), stdin=_NO_STDIN, capture_output=True, text=True)
        return subprocess.run(self._argv(argv), input=input_text, capture_output=True, text=True)

    def popen(
        self,
        argv: list[str],
        stdout: int = subprocess.PIPE,
        stderr: int = subprocess.PIPE,
        text: bool = True,
    ) -> subprocess.Popen:
        """A streaming child (e.g. `tail -f`) — caller must `.kill()` it, an
        ssh child is not reaped on drop. Never inherits our stdin, see _NO_STDIN."""
        return subprocess.Popen(self._argv(argv), stdin=_NO_STDIN, stdout=stdout, stderr=stderr, text=text)

    def read_text(self, path: str | Path) -> str:
        if self.is_local:
            return Path(path).read_text()
        return self.run(["cat", str(path)]).stdout

    def stat_size(self, path: str | Path) -> int | None:
        if self.is_local:
            try:
                return Path(path).stat().st_size
            except OSError:
                return None
        out = self.run(["stat", "-c", "%s", str(path)]).stdout.strip()
        return int(out) if out.isdigit() else None

    def stat_mtime(self, path: str | Path) -> float | None:
        if self.is_local:
            try:
                return Path(path).stat().st_mtime
            except OSError:
                return None
        out = self.run(["stat", "-c", "%Y", str(path)]).stdout.strip()
        return float(out) if out.isdigit() else None

    def is_file(self, path: str | Path) -> bool:
        if self.is_local:
            return Path(path).is_file()
        return self.run(["test", "-f", str(path)]).returncode == 0

    def glob(self, pattern: str) -> list[str]:
        """Sorted matches for `pattern` — its own glob call locally, a
        remote shell's expansion (not ours) for the ssh branch, since only
        the remote shell can see the remote filesystem."""
        if self.is_local:
            return sorted(_glob.glob(pattern))
        out = self.run(["sh", "-c", f"ls -1d {pattern} 2>/dev/null"]).stdout
        return sorted(line for line in out.splitlines() if line)


LOCAL = Host()  # the common case, as a singleton default arg (Host() itself is fine as a
# default per ruff/B008 since it's frozen, but a shared instance avoids the churn)


def close_control_master(host: Host) -> None:
    """Tear down this run's ssh ControlMaster on a graceful quit, instead of
    leaving it (and its socket file) around for the full ControlPersist=600
    window. Best-effort and silent: a hard `kill -9` never reaches this at
    all (no code runs), so it's the common-case cleanup, not the only one.
    """
    if host.alias is None:
        return

    port_opts = ["-p", str(host.port)] if host.port else []
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={_CONTROL_PATH}", *port_opts, host.alias],
            stdin=_NO_STDIN,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
