"""Planner regressions use synthetic exports and a fake calibrator, never a GPU."""

import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from odoo_activity import mcp_server


def _synthetic_records(work):
    records = []
    for (rounds, salt), count in work.items():
        encoded_salt = base64.b64encode(salt.encode()).decode()
        for _ in range(count):
            uid = len(records) + 1
            digest = base64.b64encode(uid.to_bytes(64, "big")).decode()
            records.append(f"{uid}=synthetic:sha512:{rounds}:{encoded_salt}:{digest}\n")
    return "".join(records)


@pytest.fixture
def planner(monkeypatch, tmp_path):
    calls = []
    clock = [0.0]

    def calibrate(rounds, *, timeout_s, n_salts):
        calls.append(rounds)
        return 1.0, ""

    pa = SimpleNamespace(
        calibrate=calibrate,
        build_candidates=lambda *_: ["fallback"],
        tier2_candidates=lambda: ["fallback"],
        estimate_candidate_seconds=lambda c, n, speed, salts: c * salts / speed,
    )
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: clock[0])

    def export(label, work=None, t1=1, t2=1, directory=None):
        base = directory or tmp_path
        base.mkdir(exist_ok=True)
        path = base / f"{label}.hashcat"
        path.write_text(_synthetic_records(work or {(100, "salt"): 1}))
        path.with_suffix(".derived.txt").write_text("candidate\n" * t1)
        path.with_suffix(".tier2.txt").write_text("candidate\n" * t2)
        return {"hash_file": str(path)}

    def delay_read(path, seconds):
        original = os.fdopen

        class SlowRead:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def readline(self, size=-1):
                clock[0] += seconds
                return self.stream.readline(size)

        def slow_open(fd, *args, **kwargs):
            matches = os.fstat(fd).st_ino == path.stat().st_ino
            stream = original(fd, *args, **kwargs)
            return SlowRead(stream) if matches else stream

        monkeypatch.setattr(os, "fdopen", slow_open)

    return SimpleNamespace(export=export, pa=pa, calls=calls, clock=clock, delay_read=delay_read)


def test_tier2_sizes_follow_reordered_database_paths(planner):
    small = planner.export("small", t2=100)
    large = planner.export("large", work={(100, "a"): 1, (100, "b"): 1}, t2=1)
    result = mcp_server.password_plan([small, large], 1)
    assert "tier 2 for large (2 hashes, 100 rounds): ~2s" in result
    assert "tier 2 for small: SKIPPED" in result


def test_tier2_sizes_follow_paths_after_unfunded_database(planner):
    expensive = planner.export("expensive", t1=100, t2=100)
    cheap = planner.export("cheap", t2=3)
    result = mcp_server.password_plan([expensive, cheap], 10 / 60)
    assert "tier 2 for cheap (1 hashes, 100 rounds): ~3s" in result


def test_summary_charges_only_funded_passes(planner):
    expensive = planner.export("expensive", t1=100)
    cheap = planner.export("cheap", t1=1, t2=3)
    result = mcp_server.password_plan([expensive, cheap], 10 / 60)
    assert "tier1+tier2 est 4s" in result
    assert "leftover 6s" in result
    assert "leftover -" not in result


@pytest.mark.parametrize("minutes", [0, -1, float("nan"), float("inf"), -float("inf"), 1e308])
@pytest.mark.parametrize("tool", ["password_plan", "password_crack"])
def test_invalid_minutes_rejected_before_work(planner, minutes, tool):
    audit = planner.export("db")

    def no_work(*_args, **_kwargs):
        pytest.fail("invalid budget reached calibration/hashcat")

    planner.pa.calibrate = no_work
    planner.pa.run_hashcat = no_work
    planner.pa.RUNTIME_BUDGET_S = 250
    with pytest.raises(ValueError, match=r"minutes.*finite.*positive"):
        if tool == "password_plan":
            mcp_server.password_plan([audit], minutes)
        else:
            mcp_server.password_crack(audit["hash_file"], wordlist="synthetic.txt", minutes=minutes)


@pytest.mark.parametrize("minutes, seconds", [(0.001, 1), (1.001, 61)])
def test_crack_rounds_positive_minutes_up_to_seconds(planner, minutes, seconds):
    audit = planner.export("db")
    seen = {}
    planner.pa.RUNTIME_BUDGET_S = 250

    def run(_path, **kwargs):
        seen.update(kwargs)
        return "launched", ""

    planner.pa.run_hashcat = run
    assert mcp_server.password_crack(audit["hash_file"], "synthetic.txt", minutes=minutes) == "launched"
    assert seen["runtime_budget"] == seconds


def test_mixed_rounds_use_every_unique_salt_group(planner):
    audit = planner.export("mixed", work={(100, "same"): 3, (200, "same"): 1, (200, "other"): 1}, t1=10, t2=1)

    def calibrate(rounds, *, timeout_s, n_salts):
        planner.calls.append(rounds)
        return (10.0 if rounds == 100 else 1.0), ""

    planner.pa.calibrate = calibrate
    result = mcp_server.password_plan([audit], 1)
    assert planner.calls == [100, 200]
    assert "tier 1 (prioritized): ~21s funded" in result
    assert "tier1+tier2 est 24s" in result


def test_duplicate_digests_do_not_add_pbkdf2_work_or_salt_penalty(planner):
    audit = planner.export("shared", work={(100, "salt"): 40}, t1=2, t2=3)
    # A legacy estimator's row-count/derating model must not be consulted.
    planner.pa.estimate_candidate_seconds = lambda *_args: 10000.0
    result = mcp_server.password_plan([audit], 10 / 60)
    assert "tier 1 (prioritized): ~2s funded" in result
    assert "tier1+tier2 est 5s" in result


def test_calibration_wall_time_including_setup_is_charged(planner):
    audit = planner.export("db", t1=3, t2=3)

    def calibrate(rounds, *, timeout_s, n_salts):
        planner.clock[0] += 5.0  # includes hashcat startup, not just benchmark compute
        return 1.0, ""

    planner.pa.calibrate = calibrate
    result = mcp_server.password_plan([audit], 10 / 60)
    assert "calibration + planning elapsed: 5s" in result
    assert "tier 2 for db: SKIPPED" in result
    assert "leftover 2s" in result


@pytest.mark.parametrize("minutes, expected_timeouts", [(1 / 60, [0.5]), (60, [245.0, 125.0, 5.0])])
def test_many_rounds_stop_calibrating_at_planning_deadline(planner, minutes, expected_timeouts):
    audit = planner.export("mixed", work={(r, "salt"): 1 for r in range(100, 110)})
    timeouts = []

    def calibrate(rounds, *, timeout_s=None, n_salts):
        planner.calls.append(rounds)
        timeouts.append(timeout_s)
        planner.clock[0] += 120.0
        return 1.0, ""

    planner.pa.calibrate = calibrate
    with pytest.raises(ValueError, match=r"(?i)planning.*(?:budget|time.limit)") as error:
        mcp_server.password_plan([audit], minutes)
    assert planner.calls == list(range(100, 100 + len(expected_timeouts)))
    assert timeouts == expected_timeouts
    assert all(0 < timeout <= 250 for timeout in timeouts)
    assert "no valid plan" in str(error.value)
    elapsed = planner.clock[0]
    remaining = max(0, minutes * 60 - elapsed)
    assert f"{elapsed:,.0f}s (charged to budget)" in str(error.value)
    assert f"audit remaining {remaining:,.0f}s" in str(error.value)


def test_calibration_timeout_degrades_to_unsized_plan(planner):
    audit = planner.export("mixed", work={(100, "a"): 1, (200, "b"): 1})

    def calibrate(rounds, *, timeout_s, n_salts):
        planner.calls.append(rounds)
        planner.clock[0] += 2.0
        return None, "calibration time limit: CPU preparation worker timed out"

    planner.pa.calibrate = calibrate
    result = mcp_server.password_plan([audit], 10 / 60)
    assert planner.calls == [100]
    assert "unmeasured at 100 rounds" in result
    assert "100, 200 rounds; the plan is unsized" in result
    assert "tier 1" in result
    # Unsized plan: every database gets a tier-1 pass with an explicit call,
    # no per-pass seconds estimate, and no tier-2.
    assert "UNFUNDED" not in result
    assert "tier 2 is skipped" in result
    assert "password_crack(hash_file=" in result
    assert "unsized" in result.lower()


def test_partial_calibration_failure_sizes_measured_rounds_only(planner):
    audit = planner.export("mixed", work={(100, "a"): 1, (200, "b"): 1})

    def calibrate(rounds, *, timeout_s, n_salts):
        planner.calls.append(rounds)
        if rounds == 200:
            return None, "calibration time limit"
        return 2.0, ""

    planner.pa.calibrate = calibrate
    result = mcp_server.password_plan([audit], 10)
    assert "candidate-salt pairs/s at 100 rounds" in result
    assert "unmeasured at 200 rounds" in result
    assert "ESTIMATES UNAVAILABLE" in result


def test_exhausted_file_read_budget_starts_no_calibration(planner):
    audit = planner.export("db")
    planner.delay_read(Path(audit["hash_file"]), 1.0)
    with pytest.raises(ValueError, match="planning budget/time limit exhausted"):
        mcp_server.password_plan([audit], 1 / 60)
    assert planner.calls == []


def test_exhausted_calibration_budget_rejects_even_last_probe(planner):
    audit = planner.export("db")

    def calibrate(_rounds, *, timeout_s, n_salts):
        planner.clock[0] += 12.0
        return 1.0, ""

    planner.pa.calibrate = calibrate
    with pytest.raises(ValueError, match="planning budget/time limit exhausted") as error:
        mcp_server.password_plan([audit], 10 / 60)
    assert "audit remaining 0s" in str(error.value)
    assert "no valid plan" in str(error.value)


def test_planning_call_overhead_is_charged(planner):
    audit = planner.export("db", t1=3, t2=3)
    # One hash record and its EOF read consume five seconds in total.
    planner.delay_read(Path(audit["hash_file"]), 2.5)
    result = mcp_server.password_plan([audit], 10 / 60)
    assert "tier 2 for db: SKIPPED" in result
    assert "leftover 2s" in result


@pytest.mark.parametrize("host,port,identity", [(None, None, "local"), ("ops@prod", 2222, "ops@prod:2222")])
def test_exports_reserve_unique_paths_with_host_identity(monkeypatch, tmp_path, host, port, identity):
    calls = []

    def destination(instance, db, host_identity="local"):
        calls.append((instance, db, host_identity))
        return tmp_path / f"export-{len(calls)}.hashcat"

    pa = SimpleNamespace(
        WORK_DIR=tmp_path,
        HASHCAT_MODE=12100,
        export_path=destination,
        fetch_hash_rows=lambda *_: ([], ""),
        fetch_company_names=lambda *_: ([], ""),
        write_hashcat_file=lambda _rows, path, **_kw: path.write_text("synthetic\n") and 1,
    )
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "instance"})
    monkeypatch.setattr(mcp_server.probes, "pg_target_of", lambda *_: None)
    monkeypatch.setattr(mcp_server, "_pinned_target", None)
    monkeypatch.setattr(mcp_server, "_host_filter", None)
    first = mcp_server.password_hashes("instance", "db", host=host, ssh_port=port)
    second = mcp_server.password_hashes("instance", "db", host=host, ssh_port=port)
    assert calls == [("instance", "db", identity)] * 2
    assert first.splitlines()[0] != second.splitlines()[0]
    assert "\\n" not in first
    assert str(tmp_path / "export-1.loginmap.json") in first


def test_same_basename_exports_have_explicit_path_labels(planner, tmp_path):
    first = planner.export("db", t1=2, t2=3, directory=tmp_path / "first")
    second = planner.export("db", t1=4, t2=5, directory=tmp_path / "second")
    result = mcp_server.password_plan([first, second], 1)
    assert f"tier 1 for {first['hash_file']} (1 hashes, 100 rounds): ~2s" in result
    assert f"tier 1 for {second['hash_file']} (1 hashes, 100 rounds): ~4s" in result
    assert f"tier 2 for {first['hash_file']} (1 hashes, 100 rounds): ~3s" in result
    assert f"tier 2 for {second['hash_file']} (1 hashes, 100 rounds): ~5s" in result


def test_duplicate_records_use_real_planner_group_reader(planner):
    audit = planner.export("duplicates", t1=2, t2=3)
    Path(audit["hash_file"]).write_text(
        "1=user:sha512:100:salt:digest\n2=user:sha512:100:salt:digest\n3=other:sha512:100:salt:otherdigest\n"
    )
    result = mcp_server.password_plan([audit], 10 / 60)
    assert "(2 hashes, 100 rounds): ~2s" in result
    assert "tier1+tier2 est 5s" in result


def test_launch_time_gate_still_blocks_planning_and_exports(monkeypatch):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", set())
    with pytest.raises(ValueError, match="password-audit disabled"):
        mcp_server.password_plan([], 1)
    with pytest.raises(ValueError, match="password-audit disabled"):
        mcp_server.password_hashes("synthetic", "db")


def test_allocation_cannot_publish_after_planning_deadline(planner, monkeypatch):
    audit = planner.export("db")
    original = mcp_server._allocate_tier1

    def allocate(*args):
        result = original(*args)
        planner.clock[0] += 251.0
        return result

    monkeypatch.setattr(mcp_server, "_allocate_tier1", allocate)
    with pytest.raises(ValueError, match=r"planning budget/time limit exhausted.*no valid plan") as error:
        mcp_server.password_plan([audit], 60)
    assert "elapsed: 251s (charged to budget)" in str(error.value)
    assert "audit remaining 3,349s" in str(error.value)


def test_allocation_elapsed_is_charged_in_final_summary(planner, monkeypatch):
    audit = planner.export("db", t1=3, t2=3)
    original = mcp_server._allocate_tier1

    def allocate(*args):
        result = original(*args)
        planner.clock[0] += 2.0
        return result

    monkeypatch.setattr(mcp_server, "_allocate_tier1", allocate)
    result = mcp_server.password_plan([audit], 10 / 60)
    assert "calibration + planning elapsed: 2s (charged to budget)" in result
    assert "calibration+planning 2s — leftover 2s" in result
    assert "tier 2 fit: 3s of 5s remaining" in result


@pytest.mark.parametrize("tier1,tier2", [(10, 1), (3, 7)])
@pytest.mark.parametrize("delay_at", ["allocation", "output"])
def test_final_elapsed_invalidates_overfunded_plan(planner, monkeypatch, tier1, tier2, delay_at):
    audit = planner.export("db", t1=tier1, t2=tier2)
    if delay_at == "allocation":
        original = mcp_server._allocate_tier1

        def allocate(*args):
            result = original(*args)
            planner.clock[0] += 0.25
            return result

        monkeypatch.setattr(mcp_server, "_allocate_tier1", allocate)
    else:
        original = mcp_server._finalize_password_plan

        def finalize(*args):
            planner.clock[0] += 0.25  # output preparation after pass allocation
            return original(*args)

        monkeypatch.setattr(mcp_server, "_finalize_password_plan", finalize)

    with pytest.raises(ValueError, match=r"no valid plan.*funded.*remaining.*[Rr]eplan"):
        mcp_server.password_plan([audit], 10 / 60)


def test_tiny_positive_plan_budget_is_one_second(planner):
    audit = planner.export("db")
    result = mcp_server.password_plan([audit], 0.001)
    assert "1/1 dbs" in result
    assert "tier 2 for db: SKIPPED" in result
    assert "leftover 0s" in result
