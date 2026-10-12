"""Runtime review regressions: synthetic files/processes only, never a GPU."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from odoo_activity.plugins import password_audit as pa
from odoo_activity.plugins.password_audit import worker


@pytest.mark.parametrize("kind", ["hardlink", "symlink", "fifo", "public", "nonowned"])
def test_out_validation_precedes_truncation(tmp_path, monkeypatch, kind):
    target = tmp_path / "sentinel"
    pa._safe_write(target, "do not truncate")
    out = tmp_path / "audit.out"
    if kind == "hardlink":
        os.link(target, out)
    elif kind == "symlink":
        out.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(out, 0o600)
    else:
        pa._safe_write(out, "do not truncate")
        if kind == "public":
            out.chmod(0o644)
        else:
            real_fstat = os.fstat
            inode = out.stat().st_ino

            def fstat(fd):
                info = real_fstat(fd)
                if info.st_ino == inode:
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1, st_nlink=info.st_nlink)
                return info

            monkeypatch.setattr(worker.os, "fstat", fstat)
    started = time.monotonic()
    with pytest.raises(OSError):
        pa._clear_out_file(out)
    assert time.monotonic() - started < 0.5
    assert target.read_text() == "do not truncate"
    if kind in ("public", "nonowned"):
        assert out.read_text() == "do not truncate"


def test_out_validated_descriptor_is_truncated(tmp_path):
    out = tmp_path / "audit.out"
    pa._safe_write(out, "old")
    pa._clear_out_file(out)
    assert out.read_bytes() == b""
    assert out.stat().st_mode & 0o777 == 0o600


def test_equal_content_exports_have_separate_restore_namespaces(tmp_path):
    first, second, words = tmp_path / "one.hashcat", tmp_path / "two.hashcat", tmp_path / "words"
    first.write_text("synthetic hashes")
    second.write_bytes(first.read_bytes())
    words.write_text("synthetic candidates")
    a = pa._attack_snapshot(first, words, None)
    pa._safe_write(a[3], "synthetic checkpoint")
    b = pa._attack_snapshot(second, words, None)
    assert a[0] == b[0]  # same attack content, different export identity
    assert a[1][0].parent != b[1][0].parent
    assert a[2] != b[2]
    assert a[3] != b[3]
    assert not b[4]
    assert pa._attack_snapshot(first, words, None)[4]


@pytest.mark.parametrize("rc", [0, 1, 3, 4])
def test_completed_zero_hit_findings_without_potfile_are_durable(tmp_path, rc):
    hf = tmp_path / "empty.hashcat"
    hf.write_text("1=alice:sha512:1000:salt:digest\n")
    pa._safe_write(pa._status_path(hf), json.dumps({"returncode": rc}))
    assert pa.password_findings(hf) == ([], "")
    assert pa.password_findings(hf) == ([], "")
    assert pa.crack_status(hf) == ("(pass finished, no cracks)", "")


@pytest.mark.parametrize("rc", [None, -1, 2])
def test_missing_potfile_is_not_success_without_completed_pass(tmp_path, rc):
    hf = tmp_path / "empty.hashcat"
    hf.write_text("1=alice:sha512:1000:salt:digest\n")
    pa._safe_write(pa._status_path(hf), json.dumps({"returncode": rc}))
    result, error = pa.password_findings(hf)
    assert result is None and error


@pytest.mark.parametrize("reader", ["status", "show"])
@pytest.mark.parametrize("password", ["  secret \t ", " \t ", ""])
def test_status_and_show_preserve_password_whitespace(tmp_path, monkeypatch, reader, password):
    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:1000:salt:digest\n")
    output = f"sha512:1000:salt:digest:{password}\n"
    if reader == "status":
        pa._safe_write(pa._status_path(hf), '{"returncode": 1}')
        pa._safe_write(hf.with_suffix(".out"), output)
        pa._safe_write(pa._potfile_path(hf), output)
        result, error = pa.crack_status(hf)
    else:
        monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=output, stderr=""))
        result, error = pa.run_hashcat(hf)
    assert not error
    assert result == f"1=alice:sha512:1000:salt:digest:{password}"


@pytest.mark.parametrize("salts", [1, 3, 8])
def test_calibration_uses_supplied_salt_work_groups(tmp_path, monkeypatch, salts):
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    clock = iter([10.0, 10.0, 10.0, 12.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    def run(cmd, **kwargs):
        lines = Path(cmd[cmd.index("-a") + 2]).read_text().splitlines()
        assert len(lines) == salts
        assert len({line.split(":")[3] for line in lines}) == salts
        return SimpleNamespace(returncode=1, stdout=f"Progress.........: {salts * 200}/{salts * 200}", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert pa.calibrate(1000, n_salts=salts) == (salts * 100.0, "")


@pytest.mark.parametrize("salts", [0, -1, 9, True, 1.5, "2"])
def test_calibration_rejects_unbounded_or_noninteger_salt_count(salts):
    speed, error = pa.calibrate(1000, n_salts=salts)
    assert speed is None and "n_salts" in error


def test_supervisor_deadline_includes_status_loading(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    pa._safe_write(
        status, json.dumps({"command": [sys.executable, "-c", "pass"], "log": str(tmp_path / "log"), "budget_s": 0.1})
    )
    read_text = Path.read_text

    def slow_read(self, *args, **kwargs):
        result = read_text(self, *args, **kwargs)
        if self == status:
            time.sleep(0.15)
        return result

    monkeypatch.setattr(Path, "read_text", slow_read)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("expired setup spawned child"))
    worker.supervise(status, os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600))
    record = json.loads(read_text(status))
    assert record["returncode"] == -1 and "timeout" in record["error"]


def test_checkpoint_shutdown_is_reserved_inside_hard_deadline(tmp_path):
    checkpoint = tmp_path / "checkpoint"
    status = tmp_path / "status.json"
    code = (
        "import sys,time,pathlib; "
        "assert sys.stdin.read(1)=='c'; "
        "time.sleep(0.04); "
        f"pathlib.Path({str(checkpoint)!r}).write_text('saved'); sys.exit(3)"
    )
    pa._safe_write(
        status, json.dumps({"command": [sys.executable, "-c", code], "log": str(tmp_path / "log"), "budget_s": 0.5})
    )
    started = time.monotonic()
    worker.supervise(status, os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600))
    elapsed = time.monotonic() - started
    record = json.loads(status.read_text())
    assert record["returncode"] == 3, record
    assert checkpoint.read_text() == "saved"
    assert elapsed < 0.6  # only scheduler tolerance, no external shutdown grace


def test_launch_snapshot_fifo_rejected_without_hanging_runner(tmp_path):
    hf, words = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic")
    os.mkfifo(words, 0o600)
    code = (
        "import shutil; from pathlib import Path; "
        "from odoo_activity.plugins import password_audit as pa; "
        "shutil.which=lambda _: '/fake/hashcat'; "
        f"result,error=pa.launch_hashcat(Path({str(hf)!r}),Path({str(words)!r}),runtime_budget=1); "
        "assert result is None and 'regular' in error, (result,error); "
        f"import os; os.close(pa._audit_lock(Path({str(hf)!r})))"
    )
    try:
        result = subprocess.run(  # noqa: S603 - synthetic self-test child, fixed code string
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=4
        )
    except subprocess.TimeoutExpired:
        pytest.fail("FIFO snapshot blocked launcher (outer guard killed/reaped test child)")
    assert result.returncode == 0, result.stderr


def test_launch_snapshot_oversized_input_fails_fast_without_spawning(tmp_path, monkeypatch):
    hf, words = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic")
    words.write_bytes(b"x" * 4096)
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    monkeypatch.setattr(pa, "_MAX_SNAPSHOT_BYTES", 1024)
    spawned = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        spawned.append(real_popen(*args, **kwargs))
        return spawned[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    result, error = pa.launch_hashcat(hf, words, runtime_budget=1)
    assert result is None and "snapshot budget" in error, error
    assert not spawned  # rejected before any supervisor is launched
    fd = pa._audit_lock(hf)  # lock released on failure
    os.close(fd)


def test_launch_snapshot_slow_input_killed_at_deadline_and_reaped(tmp_path, monkeypatch):
    import fcntl

    hf, words = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic")
    words.write_text("synthetic")
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    real_read = pa._validated_input_bytes

    def slow_read(path):
        time.sleep(5)
        return real_read(path)

    monkeypatch.setattr(pa, "_validated_input_bytes", slow_read)
    started = time.monotonic()
    result, error = pa.launch_hashcat(hf, words, runtime_budget=1)
    elapsed = time.monotonic() - started
    assert result is None and "timeout" in error, error
    assert elapsed < 2.5  # the forked reader was SIGKILLed at the budget, not waited out
    fd = pa._audit_lock(hf)  # no orphan reader holds the audit lock
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(fd)


def test_launch_setup_is_charged_to_wall_cap(tmp_path, monkeypatch):
    hf, words = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic")
    words.write_text("synthetic")
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    snapshot = pa._attack_snapshot

    def slow_snapshot(*args):
        result = snapshot(*args)
        time.sleep(1.05)
        return result

    monkeypatch.setattr(pa, "_attack_snapshot", slow_snapshot)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("expired launcher setup spawned worker"))
    result, error = pa.launch_hashcat(hf, words, runtime_budget=1)
    assert result is None and "timeout" in error


def test_startup_timeout_kills_and_reaps_owned_starter(tmp_path, monkeypatch):
    import select

    hf, words = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic")
    words.write_text("synthetic")
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    real_popen = subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        # The snapshot fork finished (it needs the real select); starve the
        # supervisor STARTUP handshake only.
        monkeypatch.setattr(select, "select", lambda *args: ([], [], []))
        real_wait = child.wait

        def wait(timeout=None):
            if timeout is not None:
                command = "synthetic starter"
                raise subprocess.TimeoutExpired(command, timeout)
            return real_wait(timeout=timeout)

        monkeypatch.setattr(child, "wait", wait)
        return child

    monkeypatch.setattr(subprocess, "Popen", popen)
    result, error = pa.launch_hashcat(hf, words)
    try:
        assert result is None and error
        assert children[0].returncode is not None  # explicit kill and reap
        record = json.loads(pa._status_path(hf).read_text())
        assert record["returncode"] == -1 and record["error"]
        fd = pa._audit_lock(hf)
        os.close(fd)
    finally:
        if children[0].poll() is None:
            children[0].kill()
            children[0].wait()


def test_startup_handshake_retains_active_lock_and_status(tmp_path, monkeypatch):
    fake = tmp_path / "hashcat"
    fake.write_text("#!/usr/bin/env python3\nimport time,sys\ntime.sleep(0.25)\nsys.exit(1)\n")
    fake.chmod(0o700)
    monkeypatch.setattr(shutil, "which", lambda _: str(fake))
    hf, words = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic")
    words.write_text("synthetic")
    status, error = pa.launch_hashcat(hf, words, runtime_budget=2)
    assert not error and status is not None
    running, error = pa.crack_status(hf)
    assert not error and running is not None and running.startswith("running:")
    with pytest.raises(BlockingIOError):
        pa._audit_lock(hf)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result, error = pa.crack_status(hf)
        if result is not None and not result.startswith("running:"):
            break
        time.sleep(0.02)
    assert (result, error) == ("(pass finished, no cracks)", "")
    assert json.loads(status.read_text())["returncode"] == 1
    assert pa.password_findings(hf) == ([], "")


@pytest.mark.parametrize("with_hit", [False, True])
def test_checkpoint_completion_through_actual_mcp_tools(tmp_path, monkeypatch, with_hit):
    from odoo_activity import mcp_server

    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:1000:salt:digest\n")
    pa._safe_write(pa._status_path(hf), '{"returncode": 3}')
    if with_hit:
        pa._safe_write(pa._potfile_path(hf), "sha512:1000:salt:digest:synthetic\n")
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    expected = "1=alice:sha512:1000:salt:digest:synthetic" if with_hit else "(pass finished, no cracks)"
    for _ in range(2):
        assert mcp_server.password_crack_status(str(hf)) == expected
        findings = json.loads(mcp_server.password_findings(str(hf)))
        assert len(findings) == int(with_hit)
        if with_hit:
            assert findings[0]["password"] == "synthetic"  # noqa: S105 - synthetic, not a credential


def test_launch_hashcat_runtime_reserves_shutdown_inside_budget(tmp_path, monkeypatch):
    hf, words = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic")
    words.write_text("synthetic")
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    captured = []

    def popen(*args, **kwargs):
        captured.append(json.loads(pa._status_path(hf).read_text()))
        message = "synthetic spawn failure"
        raise OSError(message)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert pa.launch_hashcat(hf, words, runtime_budget=10)[1]
    command = captured[0]["command"]
    assert 1 <= int(command[command.index("--runtime") + 1]) <= 8
