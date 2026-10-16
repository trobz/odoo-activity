"""Internal detached supervisor. Not a public audit CLI or consent boundary.

The MCP-gated plugin passes an already-held flock descriptor. The supervisor
holds it until hashcat is waited/reaped and its exact exit code atomically
persisted. Status never infers success from log text or PID existence.
"""

from __future__ import annotations

import json
import math
import os
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def private_open(path: Path, flags: int = os.O_RDONLY) -> int:
    """No-follow open, validate BEFORE truncation/read/write (including FIFOs)."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            msg = f"unsafe nonprivate audit artifact: {path}"
            raise PermissionError(msg)  # noqa: TRY301 - close fd on validation failure
    except Exception:
        os.close(fd)
        raise
    return fd


def validate_private(path: Path, *, missing_ok: bool = False) -> bool:
    try:
        fd = private_open(path)
    except FileNotFoundError:
        if missing_ok:
            return False
        raise
    os.close(fd)
    return True


def private_read(path: Path) -> bytes:
    with os.fdopen(private_open(path), "rb") as stream:
        return stream.read()


def atomic_json(path: Path, record: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rewrite_legacy_checkpoint(path: Path, command: list[str]) -> None:
    """Keep checkpoint state; replace ALL saved argv with current gated argv.

    Verified format: hashcat v7.1.2 include/types.h restore_data_t and
    src/restore.c read_restore/write_restore (RESTORE_VERSION_CUR=611).
    https://github.com/hashcat/hashcat/blob/v7.1.2/include/types.h
    https://github.com/hashcat/hashcat/blob/v7.1.2/src/restore.c
    Only Linux little-endian LP64 is supported: C struct is 296 bytes,
    words_cur at 272, argc at 280, argv pointer at 288. Fail closed on any
    other ABI/version or malformed argv rather than guess a binary layout.
    """
    if not sys.platform.startswith("linux") or sys.byteorder != "little" or struct.calcsize("P") != 8:
        msg = "unsupported legacy checkpoint ABI"
        raise ValueError(msg)
    data = private_read(path)
    if len(data) < 296 or struct.unpack_from("<I", data)[0] != 611:
        msg = "unsupported legacy checkpoint format/version"
        raise ValueError(msg)
    argc = struct.unpack_from("<I", data, 280)[0]
    arguments = data[296:].split(b"\n")
    cwd = data[4:260].split(b"\0", 1)[0]
    if not 1 <= argc <= 250 or len(arguments) != argc + 1 or arguments[-1] or any(b"\0" in a for a in arguments):
        msg = "malformed legacy checkpoint argv"
        raise ValueError(msg)
    if b"\0" not in data[4:260] or not os.path.isdir(cwd):
        msg = "invalid legacy checkpoint cwd"
        raise ValueError(msg)
    if not 1 <= len(command) <= 250 or any("\n" in a or "\0" in a for a in command):
        msg = "invalid current checkpoint argv"
        raise ValueError(msg)
    header = bytearray(data[:296])
    struct.pack_into("<I", header, 280, len(command))
    # Disk pointer is ignored by read_restore; never retain a process address.
    struct.pack_into("<Q", header, 288, 0)
    fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(header + b"\n".join(os.fsencode(a) for a in command) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def supervise(status: Path, lock_fd: int) -> None:  # noqa: C901 - explicit security/deadline/reap lifecycle
    started = time.monotonic()
    previous_umask = os.umask(0o077)
    record = json.loads(status.read_text())
    child = None
    try:
        budget = float(record["budget_s"])
        if isinstance(record["budget_s"], bool) or not math.isfinite(budget) or budget <= 0:
            msg = "budget_s must be finite and positive"
            raise ValueError(msg)  # noqa: TRY301 - persist supervisor failure
        deadline = min(started + budget, float(record.get("deadline_monotonic", started + budget)))
        shutdown_reserve = min(5.0, budget * 0.2)
        attack_deadline = deadline - shutdown_reserve
        # Recheck just before hashcat, not only on the MCP request path.
        command = record["command"]
        for flag in ("--potfile-path", "--restore-file-path"):
            if flag in command:
                artifact = Path(command[command.index(flag) + 1])
                validate_private(artifact, missing_ok=True)
                if flag == "--restore-file-path":
                    validate_private(Path(str(artifact) + ".new"), missing_ok=True)
        if record.get("resumed"):
            help_result = subprocess.run(  # noqa: S603 - private MCP-built argv
                [record["command"][0], "--help"],
                capture_output=True,
                text=True,
                timeout=min(5, max(0.001, deadline - time.monotonic())),
            )
            if "--restore-position" in help_result.stdout:
                record["command"] += ["--restore-position"]
            else:
                rewrite_legacy_checkpoint(Path(record["restore_path"]), record["command"])
                record["command"] = record["legacy_resume_command"]
            atomic_json(status, record)
        if time.monotonic() >= deadline:
            msg = "hashcat preparation exceeded supervisor safety timeout"
            raise TimeoutError(msg)  # noqa: TRY301 - persist preparation failure
        log_fd = private_open(Path(record["log"]), os.O_WRONLY | os.O_CREAT)
        with os.fdopen(log_fd, "w") as log:
            os.ftruncate(log.fileno(), 0)
            child = subprocess.Popen(  # noqa: S603 - private MCP-built argv
                record["command"],
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                pass_fds=(lock_fd,),
            )
            record["child_pid"] = child.pid  # diagnostic only; never used for liveness/signals
            atomic_json(status, record)
            try:
                record["returncode"] = child.wait(timeout=max(0, attack_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                # Hashcat's 'c' requests checkpoint+quit. Reserve its shutdown
                # inside the hard cap, never add grace after the deadline.
                try:
                    if child.stdin is not None:
                        child.stdin.write(b"c\n")
                        child.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    record["returncode"] = child.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    # Popen still owns this unreaped child: no PID-reuse race.
                    child.kill()
                    record["returncode"] = child.wait()
                    record["error"] = "hashcat exceeded supervisor safety timeout"
            finally:
                if child.stdin is not None:
                    child.stdin.close()
    except Exception as exc:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        record["returncode"] = -1
        record["error"] = str(exc)
    atomic_json(status, record)
    os.close(lock_fd)
    os.umask(previous_umask)


if __name__ == "__main__":
    # The intermediate process exits and is synchronously reaped by the MCP
    # launcher. The orphan supervisor is adopted by the OS child reaper and
    # itself waits hashcat; no server-owned long-lived Popen/zombie remains.
    if os.fork():
        os._exit(0)
    os.setsid()
    if len(sys.argv) == 5:
        import select

        ready_fd, go_fd = int(sys.argv[3]), int(sys.argv[4])
        try:
            os.write(ready_fd, b"R")
            os.close(ready_fd)
            ready, _, _ = select.select([go_fd], [], [], 5)
            if not ready or os.read(go_fd, 1) != b"G":
                os._exit(1)
        finally:
            os.close(go_fd)
    supervise(Path(sys.argv[1]), int(sys.argv[2]))
