"""The password-audit plugin: candidate generation, hash parsing, the
hashcat conversion, and the in-place audit — all against synthetic passlib
hashes (same shape odoo 16+ stores, low round counts for speed)."""

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from odoo_activity.plugins import password_audit as pa


def _hash(password: str, *, salt: bytes = b"testsalt", rounds: int = 1000) -> str:
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, rounds)
    ab64 = base64.b64encode(digest).decode().rstrip("=").replace("+", ".")
    salt_s = base64.b64encode(salt).decode().rstrip("=").replace("+", ".")
    return f"$pbkdf2-sha512${rounds}${salt_s}${ab64}"


# ---------------------------------------------------------------------------
# candidate generation
# ---------------------------------------------------------------------------


def test_tier2_cap_preserves_every_original_candidate_and_prioritized_leet(monkeypatch):
    monkeypatch.setattr(pa, "_years", lambda: ["2026", "2025", "2024"])
    name = " ".join(f"aeoistlbgz{n}" for n in range(20))
    original = set(pa.tier2_candidates())
    templates = (
        "{company}{year}",
        "{company}{year}!",
        "{company}{year}?",
        "{company}-{year}",
        "{year}{company}",
        "{company}",
        "{season}{year}!",
        "{season}{year}",
        "{company}{season}",
    )
    seasons = ("spring", "summer", "autumn", "winter", "Spring", "Summer", "Autumn", "Winter")
    for base in pa._company_variants([name]):
        for year in ("2026", "2025", "2024"):
            for template in templates:
                for season in seasons:
                    original.add(template.format(company=base, year=year, season=season))
    words = pa.build_tier2([name])
    assert 0 < len(set(words) - original) <= 8192
    assert original <= set(words)
    assert len(words) == len(set(words))
    assert words == pa.build_tier2([name, name])
    assert words == pa.build_tier2([name])
    variants = pa._leet_variants("aeoistlbgz" * 40)
    assert len(variants) == 128
    assert len(variants) == len(set(variants))
    assert variants == pa._leet_variants("aeoistlbgz" * 40)
    assert "4301571892" * 40 in variants
    assert "@eoistlbgz" * 40 in variants
    assert "aeo!stlbgz" * 40 in variants
    assert "aeoi$tlbgz" * 40 in variants


def test_tier2_leet_combines_types_characters_symbols_and_templates(monkeypatch):
    monkeypatch.setattr(pa, "_years", lambda: ["2026", "2025", "2024"])
    words = pa.build_tier2(["Trobz", "Acme", "Aa", "Ii", "Ss", "Olbgz"])
    expected = {
        "7r0b2",
        "tr0bz-2026",
        "2026tr0bz",
        "tr0bz2026!",
        "tr0bzwinter",
        "4cm3",
        "@cm3",
        "4@",
        "a@",
        "1!",
        "!1",
        "5$",
        "$5",
        "01892",
    }
    expected.update("tr0bz" + symbol for symbol in pa._SYMBOLS)
    assert expected <= set(words)
    assert len(words) == len(set(words))


def test_tier1_leet_additions_have_a_deterministic_per_company_cap():
    name = " ".join(f"aeoiword{n}" for n in range(80))
    original = set(pa.build_candidates([], []))
    for base in pa._company_variants([name]):
        original.add(base)
        original.update(base + symbol for symbol in pa._SYMBOLS)
    first = pa.build_candidates([name])
    assert 0 < len(set(first) - original) <= 128
    assert original <= set(first)
    assert first == pa.build_candidates([name, name])
    assert first == pa.build_candidates([name])


def test_tier1_company_leet_is_lean_and_applies_each_type_to_all_occurrences():
    words = pa.build_candidates(["Trobz Odoo Acme Ibis"], ["zoe@orbit.example"])
    assert {"tr0bz", "Tr0bz", "tr0bz!", "0d00", "Acm3", "4cme", "1b1s"} <= set(words)
    assert "Tr0bz 0d00 Acme Ibis" in words
    assert "tr0bz#" not in words
    assert "7r0bz" not in words
    assert "4cm3" not in words
    assert "tr0bz2026" not in words
    assert "tr0bzwinter" not in words
    assert "z0e@0rbit.example" not in words
    assert "admin2024" in words  # fixed common entries remain exempt
    assert len(words) == len(set(words))


# ---------------------------------------------------------------------------
# hash parsing and the audit
# ---------------------------------------------------------------------------


def test_parse_ab64_reads_odoo_shape():
    stored = _hash("admin", salt=b"0123456789ab", rounds=1000)
    scheme, rounds, salt, digest = pa._parse_ab64(stored)
    assert scheme == "pbkdf2-sha512"
    assert rounds == 1000
    assert salt == b"0123456789ab"
    assert len(digest) == 64


def test_parse_ab64_rejects_garbage():
    for stored in ("hello", "$pbkdf2-sha512$1$AA", "$md5$1$AA$BB", _hash("x")[:-6] + "AAAA"):
        with pytest.raises(ValueError):
            pa._parse_ab64(stored)


# ---------------------------------------------------------------------------
# hashcat conversion
# ---------------------------------------------------------------------------


def test_to_hashcat_line_converts_ab64():
    stored = _hash("admin", salt=b"0123456789ab", rounds=600000)
    line = pa.to_hashcat_line("42=admin", stored)
    assert line is not None
    label, algo, rounds, salt_b64, digest_b64 = line.split(":")
    assert label == "42=admin"
    assert algo == "sha512"
    assert rounds == "600000"
    # standard base64 decodes back to the original bytes
    assert base64.b64decode(salt_b64) == b"0123456789ab"
    assert len(base64.b64decode(digest_b64)) == 64


def test_to_hashcat_line_skips_non_convertible():
    assert pa.to_hashcat_line("1=a", "plaintext") is None
    assert pa.to_hashcat_line("2=b", "$pbkdf2-sha512$1000$c2FsdA$" + "A" * 88) is None


def test_write_hashcat_file(tmp_path):
    rows = [
        {"id": 1, "login": "a", "password": _hash("admin")},
        {"id": 2, "login": "b", "password": "plaintext"},
    ]
    path = tmp_path / "out.hashcat"
    count = pa.write_hashcat_file(rows, path)

    assert count == 1
    assert (tmp_path / "out.hashcat").read_text().startswith("1=a:sha512:")
    assert (tmp_path / "out.hashcat").stat().st_mode & 0o777 == 0o600


def test_candidates_cover_check_passwords_baseline():
    """The derived wordlist must be a superset of odoo-db check-passwords:
    its common list, plus every 1- and 2-character combination over
    digits/letters (the GPU absorbs what the CPU scan deliberately
    skipped: uppercase and 2-char combos)."""
    cands = pa.build_candidates([], [])
    # odoo-db's common list
    for weak in (
        "admin",
        "odoo",
        "admin123",
        "odoo123",
        "password",
        "123456",
        "1234567890",
        "azerty",
        "qwerty",
        "demo",
    ):
        assert weak in cands, weak
    # tier 1: every single char over the full alphabet
    import string

    alphabet = string.digits + string.ascii_lowercase + string.ascii_uppercase
    for ch in alphabet:
        assert ch in cands, ch
    # the 2-char combos are tier 2, NOT in tier 1
    assert "7X" not in cands
    assert len(pa.tier2_candidates()) == len(alphabet) ** 2
    assert "7X" in pa.tier2_candidates()


def test_write_hashcat_file_writes_derived_wordlist(tmp_path):
    rows = [{"id": 1, "login": "john@acme.com", "password": _hash("admin")}]
    path = tmp_path / "db1.hashcat"
    pa.write_hashcat_file(rows, path, companies=["Acme Corp"])

    derived = tmp_path / "db1.derived.txt"
    words = derived.read_text().splitlines()
    # login-derived: the login itself and its email local part
    assert "john" in words
    # tier-1 is LEAN: bare company variants + trailing-symbol mutations,
    # NO year/season mutations (those moved to tier-2 — they doubled
    # tier-1 beyond minimum coverage)
    assert "Acme Corp" in words
    assert "acmecorp" in words
    assert "Acme Corp!" in words  # trailing symbol (e.g. Lacaravane!)
    assert "acmecorp#" in words
    assert "Acme Corp2026!" not in words
    # the fixed common corpus rides along
    assert "admin" in words
    assert derived.stat().st_mode & 0o777 == 0o600
    # tier-2 companion file: 2-char combos + the mutation variants
    tier2 = tmp_path / "db1.tier2.txt"
    tier2_words = tier2.read_text().splitlines()
    assert "7X" in tier2_words and len(tier2_words) >= 62**2
    assert "Acme Corp2026!" in tier2_words
    assert "acmecorp2026" in tier2_words
    assert tier2.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# calibration + plan sizing
# ---------------------------------------------------------------------------


def test_read_hash_file_stats(tmp_path):
    """(hash count, max rounds, unique salts) parsed from a written hash
    file."""
    lines = "\n".join([
        "1=alice:sha512:600000:c2FsdDE=:YQ==",
        "2=bob:sha512:25000:c2FsdDI=:Yg==",
        "3=carol:sha512:25000:c2FsdDI=:Yw==",  # same salt as bob
    ])
    f = tmp_path / "x.hashcat"
    f.write_text(lines + "\n")
    assert pa.read_hash_file_stats(f) == (3, 600000, 2)  # 3 hashes, 2 unique salts
    # garbage lines don't count, empty file is (0, 0, 0)
    f.write_text("not-a-hash\n\n")
    assert pa.read_hash_file_stats(f) == (0, 0, 0)


def test_estimate_candidate_seconds_scales_linearly():
    """pairs/s model: cands * hashes / speed."""
    assert pa.estimate_candidate_seconds(100, 4, 50.0) == 8.0


def test_calibrate_uses_the_wordlist_attack(monkeypatch):
    """calibrate must benchmark -a 0 wordlist throughput (mask attacks
    measure a different H/s on this hardware), build the bench hashes with
    to_hashcat_line (a hand-rolled ab64 line parses as garbage), and run
    at the caller's round count."""

    seen = {}
    import time

    clock = iter([10.0, 10.0, 10.0, 11.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    def fake_run(cmd, capture_output, text, timeout):
        seen["cmd"] = cmd
        seen["timeout"] = timeout

        class P:
            returncode = 1
            stdout = "Progress.........: 1600/1600 (100.00%)\n"
            stderr = ""

        return P()

    import shutil as _shutil
    import subprocess as _subprocess

    monkeypatch.setattr(_shutil, "which", lambda _n: "/usr/bin/hashcat")
    monkeypatch.setattr(_subprocess, "run", fake_run)
    speed, err = pa.calibrate(12345)
    assert err == ""
    assert speed == 1600.0  # deterministic one-second measurement
    cmd = seen["cmd"]
    assert "-a" in cmd and "0" in cmd  # wordlist attack, not mask
    idx = cmd.index("-a")
    assert cmd[idx + 1] == "0"
    assert "--potfile-disable" in cmd
    # the bench hash file carries the probe rounds: capture write_text calls
    assert seen["timeout"] == 120


# ---------------------------------------------------------------------------
# async crack: launch detached + poll status
# ---------------------------------------------------------------------------


def test_crack_status_running_and_finished(tmp_path, monkeypatch):
    """Finished results remain durable and can be polled repeatedly."""
    import json as _json

    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:25000:c2FsdDE=:YQ==\n")
    pa._safe_write(tmp_path / "db.out", "sha512:25000:c2FsdDE=:YQ==:pw\n")
    st = tmp_path / "db.status.json"
    pa._safe_write(st, _json.dumps({"pid": 0, "started": pa.time.time() - 9999, "budget_s": 20, "returncode": 1}))
    # pid 0: os.kill(0, 0) probes the process GROUP — fake a dead process
    import os as _os

    monkeypatch.setattr(_os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    answer, err = pa.crack_status(hf)
    assert answer is not None
    assert err == "" and "1=alice" in answer and "25000" in answer
    assert st.exists()  # durable completion retained for repeated polling


def test_crack_status_zombie_pid_counts_as_finished(tmp_path, monkeypatch):
    """A detached hashcat that exited but wasn't reaped (zombie, state Z)
    must flip status to finished — kill(0) succeeds on zombies, so the
    liveness probe alone reports "running" forever (field bug)."""
    import json as _json

    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:25000:c2FsdDE=:YQ==\n")
    pa._safe_write(tmp_path / "db.out", "sha512:25000:c2FsdDE=:YQ==:pw\n")
    st = tmp_path / "db.status.json"
    pa._safe_write(st, _json.dumps({"pid": 424242, "started": pa.time.time() - 9999, "budget_s": 20, "returncode": 1}))
    # kill(0) succeeds (zombie exists), /proc reports state Z
    import os as _os

    monkeypatch.setattr(_os, "kill", lambda pid, sig: None)

    class FakeStatFile:
        def __init__(self, path):
            self._path = str(path)

        def read_text(self):
            return "424242 (hashcat) Z 1 1 0 0 -1 0 0 0 0 0 0 0 0 20 0"

        def __str__(self):
            return self._path

    from pathlib import Path as _Path

    real_read_text = _Path.read_text

    def fake_read_text(self, *a, **k):
        if "/proc/424242/stat" in str(self):
            return "424242 (hashcat) Z 1 1 0 0 -1 0 0 0 0 0 0 0 0 20 0"
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(_Path, "read_text", fake_read_text)
    answer, err = pa.crack_status(hf)
    assert answer is not None
    assert err == "" and "1=alice" in answer  # finished, not "running"


def test_crack_status_without_launch_is_an_error(tmp_path):
    ans, err = pa.crack_status(tmp_path / "nothing.hashcat")
    assert ans is None and "no launched pass" in err


def test_password_findings_joins_potfile_and_hashfile(tmp_path):
    """Structured findings: uid/login/digest/password joined from the
    audit-local potfile and the hash file; plains with colons survive."""
    hf = tmp_path / "db.hashcat"
    hf.write_text(
        "9=john:sha512:25000:c2FsdDE=:ZGlnZXN0MQ==\n"
        "11=sarah:sha512:25000:c2FsdDI=:ZGlnZXN0Mg==\n"
        "13=uncracked:sha512:25000:c2FsdDM=:ZGlnZXN0Mw==\n"
    )
    pa._safe_write(
        tmp_path / "db.potfile",
        # plains with colons must survive (field 5 = rest of line)
        "sha512:25000:c2FsdDE=:ZGlnZXN0MQ==:Acme2024!\nsha512:25000:c2FsdDI=:ZGlnZXN0Mg==:7X:with:colons\n",
    )
    findings, err = pa.password_findings(hf)
    assert err == ""
    assert findings is not None
    by_uid = {f["uid"]: f for f in findings}
    assert set(by_uid) == {9, 11}  # uncracked excluded
    assert by_uid[9]["login"] == "john" and "Acme2024" in by_uid[9]["password"]
    assert by_uid[9]["digest"] == "ZGlnZXN0MQ=="
    assert "7X" in by_uid[11]["password"] and "colons" in by_uid[11]["password"]
    assert by_uid[11]["source"] == "audit-potfile"


@pytest.mark.parametrize("reader", [pa.password_findings, pa.crack_status])
@pytest.mark.parametrize("kind", ["symlink", "public", "hardlink", "nonowned", "fifo"])
def test_readers_refuse_unsafe_potfile(tmp_path, monkeypatch, reader, kind):
    import os

    from odoo_activity.plugins.password_audit import worker

    hf = tmp_path / "db.hashcat"
    # Hash input compatibility: normal readable input need not be private.
    hf.write_text("1=alice:sha512:1000:salt:digest\n")
    hf.chmod(0o644)
    pa._safe_write(hf.with_suffix(".status.json"), '{"returncode": 1}')
    target = tmp_path / "target"
    pa._safe_write(target, "sha512:1000:salt:digest:synthetic-secret\n")
    pot = hf.with_suffix(".potfile")
    if kind == "symlink":
        pot.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, pot)
    elif kind == "fifo":
        os.mkfifo(pot, 0o600)
    else:
        pa._safe_write(pot, target.read_bytes())
        if kind == "public":
            pot.chmod(0o644)
        else:
            real_fstat = os.fstat
            inode = pot.stat().st_ino

            def nonowned(fd):
                info = real_fstat(fd)
                if info.st_ino == inode:
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1, st_nlink=info.st_nlink)
                return info

            monkeypatch.setattr(worker.os, "fstat", nonowned)
    result, error = reader(hf)
    assert result is None and error
    assert "synthetic-secret" not in error
    assert target.read_text() == "sha512:1000:salt:digest:synthetic-secret\n"


@pytest.mark.parametrize("artifact", [".status.json", ".out"])
@pytest.mark.parametrize("kind", ["symlink", "public", "hardlink", "fifo"])
def test_status_refuses_unsafe_completion_artifact(tmp_path, artifact, kind):
    import os

    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:1000:salt:digest\n")
    pa._safe_write(hf.with_suffix(".status.json"), '{"returncode": 1}')
    target = tmp_path / "target"
    body = '{"returncode": 1}' if artifact == ".status.json" else "sha512:1000:salt:digest:synthetic-secret\n"
    pa._safe_write(target, body)
    unsafe = hf.with_suffix(artifact)
    unsafe.unlink(missing_ok=True)
    if kind == "symlink":
        unsafe.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, unsafe)
    elif kind == "fifo":
        os.mkfifo(unsafe, 0o600)
    else:
        pa._safe_write(unsafe, body)
        unsafe.chmod(0o644)
    result, error = pa.crack_status(hf)
    assert result is None and error
    assert "synthetic-secret" not in error
    assert target.read_text() == body


@pytest.mark.parametrize("reader", [pa.password_findings, pa.crack_status])
@pytest.mark.parametrize("swap_at", ["open", "fstat"])
def test_potfile_read_validates_and_reads_same_descriptor(tmp_path, monkeypatch, reader, swap_at):
    import os

    from odoo_activity.plugins.password_audit import worker

    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:1000:salt:digest\n")
    pa._safe_write(hf.with_suffix(".status.json"), '{"returncode": 1}')
    pot = hf.with_suffix(".potfile")
    pa._safe_write(pot, "sha512:1000:salt:digest:safe\n")
    inode = pot.stat().st_ino
    target = tmp_path / "target"
    pa._safe_write(target, "sha512:1000:salt:digest:synthetic-secret\n")

    def swap():
        pot.unlink()
        pot.symlink_to(target)

    if swap_at == "open":
        real_open = os.open

        def swapped_open(path, flags, *args, **kwargs):
            if Path(path) == pot:
                swap()
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(worker.os, "open", swapped_open)
    else:
        real_fstat = os.fstat

        def swapped_fstat(fd):
            info = real_fstat(fd)
            if info.st_ino == inode:
                swap()
            return info

        monkeypatch.setattr(worker.os, "fstat", swapped_fstat)
    result, error = reader(hf)
    assert "synthetic-secret" not in str((result, error))
    if swap_at == "open":
        assert result is None and error
    else:
        assert not error and "safe" in str(result)


def test_password_findings_no_potfile_is_an_error(tmp_path):
    findings, err = pa.password_findings(tmp_path / "none.hashcat")
    assert findings is None and "no audit potfile" in err


def test_show_reads_stdout_not_stale_out_and_labels_once(tmp_path, monkeypatch):
    import shutil
    import subprocess

    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:1000:c2FsdA==:YQ==\n")
    hf.with_suffix(".out").write_text("stale output")
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    for output in ("sha512:1000:c2FsdA==:YQ==:pw", "1=alice:sha512:1000:c2FsdA==:YQ==:pw"):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, output=output, **k: SimpleNamespace(returncode=0, stdout=output, stderr="")
        )
        hits, err = pa.run_hashcat(hf)
        assert err == ""
        assert hits == "1=alice:sha512:1000:c2FsdA==:YQ==:pw"


def test_export_path_is_unique_private_and_host_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "WORK_DIR", tmp_path)
    first = pa.export_path("../instance", "../db", "ssh:a")
    second = pa.export_path("../instance", "../db", "ssh:a")
    other = pa.export_path("../instance", "../db", "ssh:b")
    assert len({first.parent, second.parent, other.parent}) == 3
    assert first.parent.parent != other.parent.parent
    for path in (first, second, other):
        assert path.is_relative_to(tmp_path)
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert not path.exists()
        pa.write_hashcat_file([], path)


@pytest.mark.parametrize(
    "raw,expected,encoding", [(b"colon:line\n\x00", "colon:line\n\x00", "utf-8"), (b"\xff\xfe", "fffe", "hex")]
)
def test_findings_decodes_hex_losslessly(tmp_path, raw, expected, encoding):
    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:1000:c2FsdA==:YQ==\n")
    pa._safe_write(hf.with_suffix(".potfile"), f"sha512:1000:c2FsdA==:YQ==:$HEX[{raw.hex()}]\n")
    rows, err = pa.password_findings(hf)
    assert err == ""
    assert rows is not None
    assert rows[0]["password"] == expected
    assert rows[0]["password_encoding"] == encoding


def test_supervised_job_survives_launcher_exit_and_locks_before_truncate(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    import time

    fake = tmp_path / "hashcat"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys,time,pathlib\n"
        "time.sleep(0.4)\n"
        "p=pathlib.Path(sys.argv[sys.argv.index('-o')+1]); p.write_text('1=alice:sha512:1000:c2FsdA==:YQ==:pw\\n')\n"
        "sys.exit(1)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    hf = tmp_path / "db.hashcat"
    hf.write_text("1=alice:sha512:1000:c2FsdA==:YQ==\n")
    wl = tmp_path / "words"
    wl.write_text("pw\n")
    code = (
        "from pathlib import Path; from odoo_activity.plugins import password_audit as p; "
        f"s,e=p.run_hashcat(Path({str(hf)!r}),Path({str(wl)!r}),runtime_budget=0.1); "
        "assert not e,e; assert 'LAUNCHED' in s"
    )
    launcher = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=3)  # noqa: S603
    assert launcher.returncode == 0, launcher.stderr
    hf.with_suffix(".out").write_text("sentinel")
    answer, err = pa.run_hashcat(hf, wl, runtime_budget=1)
    assert answer is None and "already running" in err
    assert hf.with_suffix(".out").read_text() == "sentinel"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = json.loads(hf.with_suffix(".status.json").read_text())
        if record.get("returncode") is not None:
            break
        time.sleep(0.02)
    assert record["returncode"] == 1
    assert record["budget_s"] == 1
    answer, err = pa.crack_status(hf)
    assert not err and answer == "1=alice:sha512:1000:c2FsdA==:YQ==:pw"
    assert pa.crack_status(hf) == (answer, err)


def test_checkpoint_resume_is_content_scoped(tmp_path, monkeypatch):
    import json
    import os
    import time

    fake = tmp_path / "hashcat"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys,pathlib,struct,os\n"
        "if '--help' in sys.argv: print('legacy hashcat'); sys.exit(0)\n"
        "p=pathlib.Path(sys.argv[sys.argv.index('--restore-file-path')+1])\n"
        "if '--restore' not in sys.argv:\n"
        " a=sys.argv; h=struct.pack('<I256sII4xQI4xQ',611,os.fsencode(os.getcwd()),0,0,123,len(a),0)\n"
        " p.write_bytes(h+b'\\n'.join(os.fsencode(x) for x in a)+b'\\n')\n"
        "sys.exit(4)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    hf, wl, rules = tmp_path / "db.hashcat", tmp_path / "words", tmp_path / "rules"
    hf.write_text("1=a:sha512:1000:c2FsdA==:YQ==\n")
    wl.write_text("pw\n")
    rules.write_text(":\n")

    def launch():
        status, err = pa.launch_hashcat(hf, wl, rules, 1)
        assert not err
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            assert status is not None
            record = json.loads(status.read_text())
            if record.get("returncode") is not None:
                return record
            time.sleep(0.02)
        pytest.fail("worker did not finish")

    first = launch()
    assert first["returncode"] == 4
    assert not first["resumed"]
    second = launch()
    assert second["resumed"] and second["returncode"] == 4
    old_session = first["session"]
    for file in (wl, rules, hf):
        file.write_text(file.read_text() + "changed\n")
        record = launch()
        assert not record["resumed"]
        assert record["session"] != old_session
        assert "--restore" not in record["command"]
        assert str(tmp_path) in record["restore_path"]
        old_session = record["session"]


def test_work_groups_count_unique_digests_and_no_salt_derating(tmp_path):
    hf = tmp_path / "db.hashcat"
    hf.write_text(
        "1=a:sha512:1000:salt:digest\n2=b:sha512:1000:salt:digest\n"
        "3=c:sha512:1000:salt:other\n4=d:sha512:2000:salt:digest\n"
        "garbage\n5=e:sha512:-1:salt:no\n"
    )
    assert pa.read_hash_work_groups(hf) == {(1000, "salt"): 2, (2000, "salt"): 1}
    assert pa.estimate_candidate_seconds(100, 4, 50.0, n_salts=1) == 8.0
    assert pa.effective_speed(50.0, 1) == 50.0


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True])
def test_invalid_runtime_does_not_launch_or_truncate(tmp_path, budget):
    hf = tmp_path / "db.hashcat"
    hf.with_suffix(".out").write_text("sentinel")
    answer, err = pa.run_hashcat(hf, tmp_path / "words", runtime_budget=budget)
    assert answer is None and "finite and positive" in err
    assert hf.with_suffix(".out").read_text() == "sentinel"


def test_status_does_not_trust_log_markers_or_reused_pid(tmp_path):
    import json
    import os

    hf = tmp_path / "db.hashcat"
    hf.write_text("1=a:sha512:1000:salt:digest\n")
    hf.with_suffix(".log").write_text("Exhausted\nRuntime limit reached\nAll hashes have been cracked")
    for rc in (None, 2):
        hf.with_suffix(".status.json").write_text(json.dumps({"pid": os.getpid(), "returncode": rc}))
        hf.with_suffix(".status.json").chmod(0o600)
        answer, err = pa.crack_status(hf)
        assert answer is None and "pass failed" in err


def test_status_is_cumulative_and_show_uses_same_persistent_lock(tmp_path, monkeypatch):
    import json
    import os
    import shutil

    hf = tmp_path / "db.hashcat"
    hf.write_text("1=a:sha512:1000:salt:digest\n2=b:sha512:1000:other:digest2\n")
    pa._safe_write(hf.with_suffix(".out"), "1=a:sha512:1000:salt:digest:pw\n")
    pa._safe_write(hf.with_suffix(".potfile"), "sha512:1000:salt:digest:pw\nsha512:1000:other:digest2:old\n")
    pa._safe_write(hf.with_suffix(".status.json"), json.dumps({"returncode": 1}))
    answer, err = pa.crack_status(hf)
    assert not err
    assert answer is not None
    assert answer.splitlines() == ["1=a:sha512:1000:salt:digest:pw", "2=b:sha512:1000:other:digest2:old"]
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    fd = pa._audit_lock(hf)
    try:
        assert "already running" in pa.run_hashcat(hf)[1]
    finally:
        os.close(fd)


def test_calibration_has_no_global_restore_side_effect(monkeypatch):
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")

    def run(cmd, **kwargs):
        assert "--restore-disable" in cmd
        return SimpleNamespace(returncode=1, stdout="Progress.........: 1600/1600", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert pa.calibrate(1000)[1] == ""


def test_calibration_deadline_includes_preparation_without_cpu_pbkdf2(monkeypatch):
    import shutil
    import subprocess
    import time

    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    monkeypatch.setattr(hashlib, "pbkdf2_hmac", lambda *a: pytest.fail("unbounded CPU probe"))
    clock = iter([10.0, 10.25, 10.25, 10.75])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    def run(cmd, **kwargs):
        assert kwargs["timeout"] == 0.75
        lines = Path(cmd[cmd.index("-a") + 2]).read_text().splitlines()
        assert len(lines) == 8
        assert all(len(base64.b64decode(line.split(":")[4])) == 64 for line in lines)
        return SimpleNamespace(returncode=1, stdout="Progress.........: 1600/1600", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert pa.calibrate(2147483647, timeout_s=1.0) == (3200.0, "")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_calibration_rejects_invalid_deadline(timeout):
    speed, error = pa.calibrate(1000, timeout_s=timeout)
    assert speed is None and "finite and positive" in error


def test_calibration_expired_setup_does_not_spawn(monkeypatch):
    import shutil
    import subprocess
    import time

    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    clock = iter([10.0, 12.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("deadline already expired"))
    speed, error = pa.calibrate(1000, timeout_s=1.0)
    assert speed is None and "timed out" in error


def test_calibration_measures_candidate_salt_pairs_without_wall_derating(monkeypatch):
    import shutil
    import subprocess
    import time
    from pathlib import Path

    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    clock = iter([10.0, 10.0, 10.0, 10.5])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    def run(cmd, **kwargs):
        hashes = Path(cmd[cmd.index("-a") + 2]).read_text().splitlines()
        assert len(hashes) == 8
        assert len({line.split(":")[3] for line in hashes}) == 8
        assert {line.split(":")[2] for line in hashes} == {"1000"}
        return SimpleNamespace(returncode=1, stdout="Progress.........: 1600/1600", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert pa.calibrate(1000) == (3200.0, "")


def test_private_directory_rejects_symlink(tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PermissionError):
        pa._ensure_dir(link)


def test_snapshot_reuse_reads_validated_descriptor_without_reopening(tmp_path, monkeypatch):
    import os

    from odoo_activity.plugins.password_audit import worker

    hf, wl = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic hashes")
    wl.write_text("synthetic words")
    fingerprint, snapshots, _, _, _ = pa._attack_snapshot(hf, wl, None)
    snapshot = snapshots[0]
    inode = snapshot.stat().st_ino
    target = tmp_path / "target"
    pa._safe_write(target, "different synthetic hashes")
    real_fstat = os.fstat

    def swap_after_validation(fd):
        info = real_fstat(fd)
        if info.st_ino == inode:
            snapshot.unlink()
            snapshot.symlink_to(target)
        return info

    monkeypatch.setattr(worker.os, "fstat", swap_after_validation)
    assert pa._attack_snapshot(hf, wl, None)[0] == fingerprint


def test_snapshot_writer_preserves_non_utf8_bytes(tmp_path):
    destination = tmp_path / "words"
    pa._safe_write(destination, b"\xff\x00\n")
    assert destination.read_bytes() == b"\xff\x00\n"


def test_modern_hashcat_resume_executes_restore_position(tmp_path, monkeypatch):
    import json
    import os
    import time

    fake = tmp_path / "hashcat"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys,pathlib\n"
        "if '--help' in sys.argv:\n"
        " print('--restore-position'); sys.exit(0)\n"
        "if '--restore' in sys.argv: sys.exit(2)\n"
        "p=pathlib.Path(sys.argv[sys.argv.index('--restore-file-path')+1]); p.write_text('checkpoint')\n"
        "sys.exit(4)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    hf, wl = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("1=a:sha512:1000:salt:digest\n")
    wl.write_bytes(b"\xff\n")
    for resumed in (False, True):
        status, err = pa.launch_hashcat(hf, wl, runtime_budget=1)
        assert not err
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            assert status is not None
            record = json.loads(status.read_text())
            if record.get("returncode") is not None:
                break
            time.sleep(0.02)
        assert record["returncode"] == 4
        assert record["resumed"] == resumed
        if resumed:
            assert "--restore-position" in record["command"]
            assert "--runtime" in record["command"]


@pytest.mark.parametrize("saved_budget,current_budget", [(1, 3), (300, 1)])
def test_legacy_resume_rewrites_documented_checkpoint_preserving_position(tmp_path, saved_budget, current_budget):
    import json
    import os
    import struct

    from odoo_activity.plugins.password_audit import worker

    checkpoint = tmp_path / "checkpoint.restore"
    # hashcat v7.1.2 types.h restore_data_t, Linux little-endian LP64:
    # version, cwd[256], dicts_pos, masks_pos, aligned words_cur, argc, argv pointer.
    saved = ["hashcat", "--runtime", str(saved_budget), "--unsafe-saved-flag"]
    header = struct.pack("<I256sII4xQI4xQ", 611, os.fsencode(tmp_path), 0, 0, 123456, len(saved), 0)
    checkpoint.write_bytes(header + b"\n".join(os.fsencode(a) for a in saved) + b"\n")
    checkpoint.chmod(0o600)
    fake = tmp_path / "hashcat"
    fake.write_text(
        "#!/usr/bin/env python3\nimport sys,pathlib,struct,time\n"
        "if '--help' in sys.argv: print('legacy hashcat'); sys.exit(0)\n"
        "assert '--restore' in sys.argv\n"
        "p=pathlib.Path(sys.argv[sys.argv.index('--restore-file-path')+1]); b=p.read_bytes()\n"
        "a=b[296:].decode().splitlines(); assert '--unsafe-saved-flag' not in a\n"
        "assert struct.unpack_from('<Q', b, 272)[0]==123456\n"
        f"assert a[a.index('--runtime')+1]=={str(current_budget)!r}\n"
        "time.sleep(float(a[a.index('--runtime')+1])-0.2); sys.exit(4)\n"
    )
    fake.chmod(0o700)
    current = [str(fake), "--runtime", str(current_budget), "--restore-file-path", str(checkpoint)]
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({
            "command": current,
            "resumed": True,
            "restore_path": str(checkpoint),
            "legacy_resume_command": [str(fake), "--restore-file-path", str(checkpoint), "--restore"],
            "log": str(tmp_path / "log"),
            "budget_s": current_budget,
        })
    )
    fd = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    worker.supervise(status, fd)
    record = json.loads(status.read_text())
    assert record["returncode"] == 4, record
    assert checkpoint.read_bytes()[:280] == header[:280]
    assert (tmp_path / "log").stat().st_mode & 0o777 == 0o600


def test_supervisor_enforces_current_wall_cap_without_grace(tmp_path):
    import json
    import os
    import sys
    import time

    from odoo_activity.plugins.password_audit import worker

    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({
            "command": [sys.executable, "-c", "import time; time.sleep(2)"],
            "log": str(tmp_path / "log"),
            "budget_s": 0.05,
        })
    )
    fd = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    started = time.monotonic()
    worker.supervise(status, fd)
    elapsed = time.monotonic() - started
    record = json.loads(status.read_text())
    assert record["returncode"] < 0 and "safety timeout" in record["error"]
    assert elapsed < 0.75


@pytest.mark.parametrize("kind", ["symlink", "public", "hardlink"])
def test_worker_refuses_unsafe_log_without_touching_target(tmp_path, kind):
    import json
    import os
    import sys

    from odoo_activity.plugins.password_audit import worker

    target = tmp_path / "target"
    target.write_text("sentinel")
    target.chmod(0o600)
    log = tmp_path / "log"
    if kind == "symlink":
        log.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, log)
    else:
        log.write_text("sentinel")
        log.chmod(0o644)
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({"command": [sys.executable, "-c", "print('unsafe write')"], "log": str(log), "budget_s": 1})
    )
    worker.supervise(status, os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600))
    assert json.loads(status.read_text())["returncode"] == -1
    assert target.read_text() == "sentinel"
    assert log.read_text() == "sentinel"


@pytest.mark.parametrize("artifact", ["hashes", "words", "checkpoint.restore", "checkpoint.restore.new", "potfile"])
@pytest.mark.parametrize("kind", ["symlink", "public"])
def test_launch_refuses_unsafe_reused_artifact_before_spawn(tmp_path, monkeypatch, artifact, kind):
    import shutil
    import subprocess

    hf, wl = tmp_path / "db.hashcat", tmp_path / "words"
    hf.write_text("synthetic hashes")
    wl.write_text("synthetic candidates")
    _, snapshots, _, checkpoint, _ = pa._attack_snapshot(hf, wl, None)
    paths = {
        "hashes": snapshots[0],
        "words": snapshots[1],
        "checkpoint.restore": checkpoint,
        "checkpoint.restore.new": Path(str(checkpoint) + ".new"),
        "potfile": hf.with_suffix(".potfile"),
    }
    unsafe = paths[artifact]
    body = unsafe.read_bytes() if unsafe.exists() else b"synthetic checkpoint"
    target = tmp_path / "target"
    target.write_bytes(body)
    target.chmod(0o600)
    if unsafe.exists():
        unsafe.unlink()
    if kind == "symlink":
        unsafe.symlink_to(target)
    else:
        unsafe.write_bytes(body)
        unsafe.chmod(0o644)
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("unsafe artifact reached hashcat"))
    status, error = pa.launch_hashcat(hf, wl)
    assert status is None and error
    assert target.read_bytes() == body


def test_show_refuses_symlink_potfile_before_hashcat(tmp_path, monkeypatch):
    import shutil
    import subprocess

    hf = tmp_path / "db.hashcat"
    hf.write_text("synthetic hashes")
    target = tmp_path / "target"
    target.write_text("sentinel")
    hf.with_suffix(".potfile").symlink_to(target)
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/hashcat")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("unsafe potfile reached hashcat"))
    result, error = pa.run_hashcat(hf)
    assert result is None and error
    assert target.read_text() == "sentinel"


@pytest.mark.parametrize("mode", ["truncated", "future", "argc", "no-newline"])
def test_legacy_checkpoint_invalid_format_fails_closed(tmp_path, mode):
    import os
    import struct

    from odoo_activity.plugins.password_audit import worker

    data = struct.pack("<I256sII4xQI4xQ", 611, os.fsencode(tmp_path), 0, 0, 99, 1, 0) + b"hashcat\n"
    if mode == "truncated":
        data = data[:280]
    elif mode == "future":
        data = struct.pack("<I", 999) + data[4:]
    elif mode == "argc":
        data = data[:280] + struct.pack("<I", 250) + data[284:]
    else:
        data = data[:-1]
    checkpoint = tmp_path / "checkpoint.restore"
    checkpoint.write_bytes(data)
    checkpoint.chmod(0o600)
    with pytest.raises(ValueError):
        worker.rewrite_legacy_checkpoint(checkpoint, ["hashcat", "--runtime", "1"])
    assert checkpoint.read_bytes() == data


def test_supervisor_safety_timeout_kills_and_reaps_owned_child(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys

    from odoo_activity.plugins.password_audit import worker

    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({
            "command": [sys.executable, "-c", "import time; time.sleep(60)"],
            "log": str(tmp_path / "log"),
            "budget_s": 1,
        })
    )
    real_popen = subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        real_wait = child.wait

        def wait(timeout=None):
            if timeout is None:
                return real_wait(timeout=0.1)
            command = "synthetic child"
            raise subprocess.TimeoutExpired(command, timeout)

        monkeypatch.setattr(child, "wait", wait)
        return child

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    fd = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    worker.supervise(status, fd)
    assert children[0].poll() is not None
    assert json.loads(status.read_text())["returncode"] < 0
    assert "safety timeout" in json.loads(status.read_text())["error"]


def test_write_hashcat_file_escapes_dangerous_logins(tmp_path):
    """A login with ':' or a newline used to make the plugin REFUSE the
    whole export (field bug on arena_ecatalog_live). Now the label is
    percent-escaped (hash-safe) and the original login is preserved in a
    .loginmap.json side file and restored by _decode_label."""
    rows = [
        {"id": 1, "login": "odd:login", "password": _hash("pw1")},
        {"id": 2, "login": "line\nbreak", "password": _hash("pw2")},
        {"id": 3, "login": "100%normal", "password": _hash("pw3")},
    ]
    path = tmp_path / "db.hashcat"
    n = pa.write_hashcat_file(rows, path)
    assert n == 3
    lines = path.read_text().splitlines()
    assert "1=odd%3Alogin:" in lines[0]
    assert "2=line%0Abreak:" in lines[1]
    assert "3=100%25normal:" in lines[2]  # % itself escaped: reversible
    import json as _json

    lmap = _json.loads((tmp_path / "db.loginmap.json").read_text())
    assert lmap == {"1": "odd:login", "2": "line\nbreak", "3": "100%normal"}
    # round trip: decode restores originals
    assert pa._decode_label(lines[0].split(":")[0]) == "1=odd:login"
    assert pa._decode_label(lines[1].split(":")[0]) == "2=line\nbreak"


def test_password_findings_returns_original_login_for_escaped(tmp_path):
    """password_findings must report the ORIGINAL login, not the escaped
    label, for exports containing escaped logins."""
    hf = tmp_path / "db.hashcat"
    hf.write_text("7=odd%3Alogin:sha512:25000:c2FsdDE=:ZGlnZXN0MQ==\n")
    pa._safe_write(tmp_path / "db.potfile", "sha512:25000:c2FsdDE=:ZGlnZXN0MQ==:pw\n")
    findings, err = pa.password_findings(hf)
    assert err == ""
    assert findings is not None
    assert findings[0]["login"] == "odd:login"


def test_fetch_hash_rows_falls_back_to_legacy_column(monkeypatch):
    """A legacy schema selects password_crypt without a modern-row probe."""
    calls = []

    def fake_psql(sql, db, params, target, host):
        calls.append(sql)
        if sql is pa._HASH_COLUMNS_SQL:
            return [{"attname": "password_crypt"}], ""
        return [
            {"id": 1976, "login": "voculi@sepec.fr", "password": "$pbkdf2-sha512$19000$abc$def", "share": False}
        ], ""

    monkeypatch.setattr(pa, "_psql_json", fake_psql)
    rows, err = pa.fetch_hash_rows(pa.PgTarget(), "db9")
    assert rows is not None
    assert err == "" and len(rows) == 1 and rows[0]["login"] == "voculi@sepec.fr"
    assert calls == [pa._HASH_COLUMNS_SQL, pa._HASH_SQL_LEGACY]


def test_fetch_hash_rows_no_fallback_when_rows_exist(monkeypatch):
    """A non-empty modern result must NOT trigger the legacy query."""
    calls = []

    def fake_psql(sql, db, params, target, host):
        calls.append(sql)
        if sql is pa._HASH_COLUMNS_SQL:
            return [], ""
        return [{"id": 1, "login": "a", "password": "$pbkdf2$1$ab$cd", "share": False}], ""

    monkeypatch.setattr(pa, "_psql_json", fake_psql)
    rows, _err = pa.fetch_hash_rows(pa.PgTarget(), "db16")
    assert rows is not None
    assert len(rows) == 1 and calls == [pa._HASH_COLUMNS_SQL, pa._HASH_SQL]


def test_fetch_hash_rows_falls_back_when_all_passwords_empty(monkeypatch):
    """Field case (showdoo 3S): a v9 db returns a NON-empty list of rows
    whose password column is all empty — the real hashes live in
    password_crypt. Schema detection selects the combined legacy query."""
    calls = []

    def fake_psql(sql, db, params, target, host):
        calls.append(sql)
        if sql is pa._HASH_COLUMNS_SQL:
            return [{"attname": "password_crypt"}], ""
        return [
            {"id": 1976, "login": "voculi@sepec.fr", "password": "$pbkdf2-sha512$19000$abc$def", "share": False}
        ], ""

    monkeypatch.setattr(pa, "_psql_json", fake_psql)
    rows, err = pa.fetch_hash_rows(pa.PgTarget(), "showdoo")
    assert rows is not None
    assert err == "" and len(rows) == 1
    assert rows[0]["password"].startswith("$pbkdf2-sha512$")
    assert calls == [pa._HASH_COLUMNS_SQL, pa._HASH_SQL_LEGACY]
