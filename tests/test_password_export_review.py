"""Credential-file regressions: hashcat conversion, label escaping, findings."""

import base64
import hashlib
import json

import pytest

from odoo_activity.plugins import password_audit as pa


def _hash(plain="synthetic"):
    salt = b"review-salt"
    digest = hashlib.pbkdf2_hmac("sha512", plain.encode(), salt, 1)

    def ab64(raw):
        return base64.b64encode(raw).decode().rstrip("=").replace("+", ".")

    return f"$pbkdf2-sha512$1${ab64(salt)}${ab64(digest)}"


_SEPARATORS = ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


@pytest.mark.parametrize("separator", _SEPARATORS)
def test_export_labels_roundtrip_all_record_separators_and_percent(tmp_path, separator):
    login = f"name:{separator}é%3A%25%=tail@example.test"
    path = tmp_path / "synthetic.hashcat"
    assert pa.write_hashcat_file([{"id": 7, "login": login, "password": _hash()}], path) == 1
    records = path.read_text().splitlines()
    assert len(records) == 1
    label, _, digest = records[0].partition(":")
    assert separator not in label
    assert "%3A" in label and "%253A" in label and "%2525" in label
    assert pa._decode_label(label) == f"7={login}"
    assert json.loads(path.with_suffix(".loginmap.json").read_text()) == {"7": login}
    plain = "plain:with\r\nUnicode\u2028\x85%3Aé"
    pa._safe_write(path.with_suffix(".potfile"), f"{digest}:$HEX[{plain.encode().hex()}]\n")
    findings, error = pa.password_findings(path)
    assert error == ""
    assert findings == [
        {
            "uid": 7,
            "login": login,
            "digest": digest.rsplit(":", 1)[1],
            "password": plain,
            "password_encoding": "utf-8",
            "source": "audit-potfile",
        }
    ]


def test_ascii_export_labels_remain_compatible(tmp_path):
    logins = ["john@example.test", "a+b!#$&'()*,-./;<>?@[]=^_`{|}~", "a b\tc", "odd:login", "100%normal"]
    expected = ["john@example.test", "a+b!#$&'()*,-./;<>?@[]=^_`{|}~", "a b\tc", "odd%3Alogin", "100%25normal"]
    path = tmp_path / "synthetic.hashcat"
    pa.write_hashcat_file(
        [{"id": uid, "login": login, "password": _hash()} for uid, login in enumerate(logins, 1)], path
    )
    assert [line.partition(":")[0] for line in path.read_text().splitlines()] == [
        f"{uid}={login}" for uid, login in enumerate(expected, 1)
    ]


@pytest.mark.parametrize("separator", _SEPARATORS)
def test_login_candidates_skip_unrepresentable_guesses_without_injection(tmp_path, separator):
    logins = [f"prefix{separator}suffix@example.test", f"valid-local@example{separator}.test", "normal@example.test"]
    baseline = pa.build_candidates()
    candidates = pa.build_candidates(logins=logins)
    common = len(pa.COMMON_WEAK) + len(pa._common_year_variants(pa._years()))
    assert candidates == [
        *baseline[:common],
        "valid-local",
        "normal@example.test",
        "normal",
        *baseline[common:],
    ]
    assert all(not any(sep in guess for sep in _SEPARATORS) for guess in candidates)
    path = tmp_path / "synthetic.hashcat"
    pa.write_hashcat_file(
        [{"id": uid, "login": login, "password": _hash()} for uid, login in enumerate(logins, 1)], path
    )
    assert path.with_suffix(".derived.txt").read_text().splitlines() == candidates
    assert "prefix" not in candidates and "suffix@example.test" not in candidates


@pytest.mark.parametrize("separator", _SEPARATORS)
def test_company_separators_normalized_in_written_wordlists(tmp_path, separator):
    company = f"Acme{separator}Orbit"
    path = tmp_path / "synthetic.hashcat"
    pa.write_hashcat_file([], path, companies=[company])
    for suffix, builder in ((".derived.txt", pa.build_candidates), (".tier2.txt", pa.build_tier2)):
        candidates = builder(companies=[company])
        written = path.with_suffix(suffix).read_text().splitlines()
        normalized = builder(companies=["Acme Orbit"])
        assert candidates == normalized
        assert written == candidates
        assert len(written) == len(set(written)) == len(candidates)
        assert all(not any(sep in word for sep in _SEPARATORS) for word in written)
    words = path.with_suffix(".derived.txt").read_text().splitlines()
    assert all(word in words for word in ("Acme Orbit", "AcmeOrbit", "Acme", "Orbit", "4cme Orbit!"))
    tier2 = path.with_suffix(".tier2.txt").read_text().splitlines()
    assert f"Acme Orbit{pa._years()[0]}!" in tier2
