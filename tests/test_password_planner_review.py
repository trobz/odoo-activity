"""Public planner review regressions; synthetic files, no GPU/database access."""

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from odoo_activity import mcp_server
from odoo_activity.plugins import password_audit


@pytest.fixture
def audit_plan(monkeypatch, tmp_path):
    clock = [0.0]
    calls = []

    def calibrate(rounds, *, timeout_s, n_salts=8):
        calls.append((rounds, timeout_s, n_salts))
        return 1.0, ""

    pa = SimpleNamespace(
        read_hash_work_groups=password_audit.read_hash_work_groups,
        calibrate=calibrate,
        build_candidates=lambda *_: ["fabricated fallback"],
        tier2_candidates=lambda: ["fabricated fallback"],
    )
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: clock[0])

    def export(label, *, salts=1, t1=1, t2=1, rounds=100):
        path = tmp_path / f"{label}.hashcat"
        path.write_text("".join(f"{i}=synthetic:sha512:{rounds}:salt{i}:digest{i}\n" for i in range(salts)))
        path.with_suffix(".derived.txt").write_text("candidate\n" * t1)
        path.with_suffix(".tier2.txt").write_text("candidate\n" * t2)
        return {"hash_file": str(path)}

    return SimpleNamespace(export=export, clock=clock, calls=calls, pa=pa)


@pytest.mark.parametrize("suffix", [".derived.txt", ".tier2.txt"])
def test_missing_companion_rejected_before_calibration(audit_plan, suffix):
    audit = audit_plan.export("db")
    Path(audit["hash_file"]).with_suffix(suffix).unlink()
    with pytest.raises(ValueError, match=r"missing.*wordlist|wordlist.*missing"):
        mcp_server.password_plan([audit], 1)
    assert audit_plan.calls == []


@pytest.mark.parametrize("salt_counts,expected", [([1, 8], 1), ([8, 12], 8), ([12, 20], 8)])
def test_calibration_uses_conservative_capped_salt_shape(audit_plan, salt_counts, expected):
    audits = [audit_plan.export(f"db{i}", salts=n) for i, n in enumerate(salt_counts)]
    result = mcp_server.password_plan(audits, 5)
    assert audit_plan.calls == [(100, 245.0, expected)]
    assert f"{expected} synthetic salts" in result
    assert "conservative" in result
    assert f"smallest actual salt-group count {min(salt_counts)}" in result


@pytest.mark.parametrize("kind", ["hash", "wordlist"])
def test_deadline_checked_between_input_records(audit_plan, monkeypatch, kind):
    audit = audit_plan.export("db", salts=3, t1=3)
    original = os.fdopen
    target = Path(audit["hash_file"])
    if kind == "wordlist":
        target = target.with_suffix(".derived.txt")
    seen = []

    class SlowRecords:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            seen.extend([None, None, None])
            audit_plan.clock[0] += 3.0
            return self.stream.read(size)

        def readline(self, size=-1):
            seen.append(None)
            audit_plan.clock[0] += 1.0
            return self.stream.readline(size)

    def slow_open(fd, *args, **kwargs):
        matches = os.fstat(fd).st_ino == target.stat().st_ino
        stream = original(fd, *args, **kwargs)
        return SlowRecords(stream) if matches else stream

    monkeypatch.setattr(os, "fdopen", slow_open)
    with pytest.raises(ValueError, match="planning budget/time limit exhausted"):
        mcp_server.password_plan([audit], 1 / 60)
    assert len(seen) == 1
    assert audit_plan.calls == []


@pytest.mark.parametrize("kind", ["hash", "wordlist"])
@pytest.mark.parametrize("limit", ["bytes", "records", "record_bytes", "files"])
def test_explicit_input_limits_fail_before_calibration(audit_plan, monkeypatch, kind, limit):
    audit = audit_plan.export("db", salts=3, t1=3)
    names = {
        "bytes": "_PASSWORD_PLAN_MAX_BYTES",
        "records": "_PASSWORD_PLAN_MAX_RECORDS",
        "record_bytes": "_PASSWORD_PLAN_MAX_RECORD_BYTES",
        "files": "_PASSWORD_PLAN_MAX_FILES",
    }
    monkeypatch.setattr(mcp_server, names[limit], 0)
    if kind == "wordlist" and limit != "files":
        Path(audit["hash_file"]).write_text("1=x:sha512:1:s:d\n")
        if limit == "records":
            monkeypatch.setattr(mcp_server, names[limit], 1)
        elif limit == "record_bytes":
            monkeypatch.setattr(mcp_server, names[limit], 20)
            Path(audit["hash_file"]).with_suffix(".derived.txt").write_text("x" * 21 + "\n")
        else:
            monkeypatch.setattr(mcp_server, names[limit], 20)
    with pytest.raises(ValueError, match="limit exceeded"):
        mcp_server.password_plan([audit], 1)
    assert audit_plan.calls == []


def test_reordered_and_skipped_passes_have_exact_inputs_and_caps(audit_plan):
    small = audit_plan.export("small", t2=100)
    large = audit_plan.export("large", salts=2, t2=1)
    unfunded = audit_plan.export("unfunded", t1=1000)
    result = mcp_server.password_plan([small, unfunded, large], 1)
    assert "tier 1 for unfunded: UNFUNDED" in result
    assert "tier 2 for small: SKIPPED" in result
    for audit, tier, runtime in [(large, 1, 2), (small, 1, 1), (large, 2, 2)]:
        suffix = ".derived.txt" if tier == 1 else ".tier2.txt"
        wordlist = str(Path(audit["hash_file"]).with_suffix(suffix))
        line = next(line for line in result.splitlines() if "password_crack(" in line and wordlist in line)
        assert repr(audit["hash_file"]) in line
        assert f"runtime cap {runtime}s" in line
        assert "minutes=" in line
    assert "actual remaining audit time" in result
    assert "never omit minutes" in result


def test_same_basename_paths_remain_unambiguous(audit_plan, tmp_path):
    first = audit_plan.export("db")
    other = tmp_path / "second"
    other.mkdir()
    second_path = other / "db.hashcat"
    second_path.write_text("1=synthetic:sha512:100:salt:digest\n")
    second_path.with_suffix(".derived.txt").write_text("candidate\n")
    second_path.with_suffix(".tier2.txt").write_text("candidate\n")
    result = mcp_server.password_plan([first, {"hash_file": str(second_path)}], 1)
    assert f"tier 1 for {first['hash_file']}" in result
    assert f"tier 1 for {second_path}" in result


def test_emitted_minutes_never_round_above_runtime_cap(audit_plan):
    audit = audit_plan.export("db", t1=31, t2=100)
    result = mcp_server.password_plan([audit], 1)
    import ast

    line = next(line.strip() for line in result.splitlines() if "password_crack(" in line)
    call = ast.parse(line.split(" — ")[0], mode="eval").body
    assert isinstance(call, ast.Call)
    minutes = next(ast.literal_eval(k.value) for k in call.keywords if k.arg == "minutes")
    assert mcp_server._minutes_seconds(minutes) == 31


def test_funded_runtime_caps_fit_even_when_estimates_are_subsecond(audit_plan):
    audit = audit_plan.export("db")
    audit_plan.pa.calibrate = lambda *_args, **_kwargs: (100.0, "")
    result = mcp_server.password_plan([audit], 1 / 60)
    assert result.count("password_crack(") == 1
    assert "tier 2 for db: SKIPPED" in result


@pytest.mark.parametrize("kind", ["hash", "wordlist"])
def test_fifo_input_rejected_without_open(audit_plan, monkeypatch, kind):
    import os

    audit = audit_plan.export("db")
    path = Path(audit["hash_file"])
    if kind == "wordlist":
        path = path.with_suffix(".derived.txt")
    path.unlink()
    os.mkfifo(path)
    original = Path.open

    def no_fifo_open(candidate, *args, **kwargs):
        assert candidate != path, "FIFO must not be opened"
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_fifo_open)
    with pytest.raises(ValueError, match=r"regular|no such"):
        mcp_server.password_plan([audit], 1)
    assert audit_plan.calls == []


@pytest.mark.parametrize("seconds", [10, 600])
def test_timeout_consumes_probe_allowance_and_returns_bounded_fallback(audit_plan, seconds):
    audits = [audit_plan.export("first"), audit_plan.export("second", rounds=200)]

    def timeout(rounds, *, timeout_s, n_salts):
        audit_plan.calls.append(rounds)
        assert timeout_s == min(seconds, 250) - 5
        audit_plan.clock[0] += timeout_s
        return None, "probe timed out"

    audit_plan.pa.calibrate = timeout
    result = mcp_server.password_plan(audits, seconds / 60)
    caps = []
    for line in result.splitlines():
        if "password_crack(" in line:
            call = ast.parse(line.strip().split(" — ")[0], mode="eval").body
            minutes = next(ast.literal_eval(k.value) for k in call.keywords if k.arg == "minutes")
            caps.append(mcp_server._minutes_seconds(minutes))
    assert audit_plan.calls == [100]
    assert len(caps) == 2
    assert all(cap > 0 for cap in caps)
    assert sum(caps) <= seconds - audit_plan.clock[0]
    assert "ESTIMATES UNAVAILABLE" in result
    assert "charged to budget" in result


@pytest.mark.parametrize("seconds,expected_calls", [(5, 1), (6, 2)])
def test_fractional_estimates_fund_and_cap_whole_seconds(audit_plan, seconds, expected_calls):
    audit = audit_plan.export("db", t1=29, t2=29)
    audit_plan.pa.calibrate = lambda *_args, **_kwargs: (10.0, "")
    result = mcp_server.password_plan([audit], seconds / 60)
    assert result.count("password_crack(") == expected_calls
    assert result.count("runtime cap 3s") == expected_calls
    assert f"tier1+tier2 est {3 * expected_calls}s" in result


@pytest.mark.parametrize("kind", ["hash", "wordlist"])
def test_path_swapped_to_fifo_before_open_is_rejected(audit_plan, monkeypatch, kind):
    audit = audit_plan.export("db")
    target = Path(audit["hash_file"])
    if kind == "wordlist":
        target = target.with_suffix(".derived.txt")
    original = os.open

    def swap(path, flags, *args, **kwargs):
        if path == target:
            assert flags & os.O_NONBLOCK
            target.unlink()
            os.mkfifo(target)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(ValueError, match="non-regular"):
        mcp_server.password_plan([audit], 1)
    assert audit_plan.calls == []


def test_fallback_after_audit_exhaustion_emits_no_attack(audit_plan):
    audit = audit_plan.export("db")

    def timeout(*_args, **_kwargs):
        audit_plan.clock[0] += 10
        return None, "timeout during cleanup"

    audit_plan.pa.calibrate = timeout
    result = mcp_server.password_plan([audit], 10 / 60)
    assert "password_crack(" not in result
    assert "UNFUNDED" in result
