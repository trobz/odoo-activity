"""SQL/export review regressions using synthetic hashes and in-memory SQL only."""

import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from odoo_activity import mcp_server
from odoo_activity.plugins import password_audit as pa


def _hash(plain="synthetic"):
    salt = b"review-salt"
    digest = hashlib.pbkdf2_hmac("sha512", plain.encode(), salt, 1)

    def ab64(raw):
        return base64.b64encode(raw).decode().rstrip("=").replace("+", ".")

    return f"$pbkdf2-sha512$1${ab64(salt)}${ab64(digest)}"


def _database(monkeypatch, *, legacy=False, rows=()):
    """Evaluate production row-selection SQL, replacing only PostgreSQL JSON wrapping."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    columns = "id integer, login text, password text, share boolean, active boolean"
    if legacy:
        columns += ", password_crypt text"
    conn.execute(f"CREATE TABLE res_users ({columns})")
    insert = (
        "INSERT INTO res_users VALUES (?, ?, ?, ?, ?, ?)" if legacy else "INSERT INTO res_users VALUES (?, ?, ?, ?, ?)"
    )
    for row in rows:
        conn.execute(insert, row)
    calls = []

    def psql(sql, db, params, target, host):
        calls.append(sql)
        if "pg_attribute" in sql:
            return ([{"attname": "password_crypt"}] if legacy else []), ""
        inner = sql.split("SELECT json_agg(t) FROM (", 1)[1].rsplit(") t", 1)[0]
        try:
            return [dict(row) for row in conn.execute(inner)], ""
        except sqlite3.Error as exc:
            return None, str(exc)

    monkeypatch.setattr(pa, "_psql_json", psql)
    return calls


def test_modern_empty_export_does_not_query_missing_legacy_column(monkeypatch):
    calls = _database(monkeypatch)
    assert pa.fetch_hash_rows(pa.PgTarget(), "synthetic") == ([], "")
    assert all("password_crypt END" not in sql for sql in calls)


@pytest.mark.parametrize("password", ["", "plaintext", "$unsupported$hash"])
def test_modern_unconvertible_rows_do_not_trigger_legacy_probe(monkeypatch, password):
    _database(monkeypatch, rows=[(1, "modern", password, False, True)])
    rows, error = pa.fetch_hash_rows(pa.PgTarget(), "synthetic")
    assert error == ""
    assert rows == [{"id": 1, "login": "modern", "password": password, "share": 0}]


def test_legacy_schema_selects_password_or_crypt_per_row(monkeypatch):
    modern, legacy = _hash("modern"), _hash("legacy")
    _database(
        monkeypatch,
        legacy=True,
        rows=[
            (1, "modern", modern, False, True, _hash("stale")),
            (2, "legacy", "", True, True, legacy),
            (3, "null", None, False, True, legacy),
            (4, "empty", "", False, True, ""),
            (5, "inactive", "", False, False, legacy),
        ],
    )
    rows, error = pa.fetch_hash_rows(pa.PgTarget(), "synthetic")
    assert error == ""
    assert rows == [
        {"id": 1, "login": "modern", "password": modern, "share": 0},
        {"id": 2, "login": "legacy", "password": legacy, "share": 1},
        {"id": 3, "login": "null", "password": legacy, "share": 0},
    ]


@pytest.mark.parametrize("failure", ["schema", "rows"])
def test_sql_failure_is_not_masked_by_fallback(monkeypatch, failure):
    calls = []
    error = "permission denied for synthetic relation"

    def psql(sql, *_args):
        calls.append(sql)
        if "pg_attribute" in sql and failure == "rows":
            return [], ""
        if len(calls) == 1 or failure == "rows":
            return None, error
        return [], ""

    monkeypatch.setattr(pa, "_psql_json", psql)
    assert pa.fetch_hash_rows(pa.PgTarget(), "synthetic") == (None, error)
    assert len(calls) == (1 if failure == "schema" else 2)


@pytest.mark.parametrize("legacy", [False, True])
def test_password_hashes_mcp_exports_modern_empty_and_mixed_legacy(monkeypatch, tmp_path, legacy):
    rows = (
        [(1, "modern", _hash("modern"), False, True, ""), (2, "legacy", "", True, True, _hash("legacy"))]
        if legacy
        else []
    )
    _database(monkeypatch, legacy=legacy, rows=rows)
    monkeypatch.setattr(pa, "WORK_DIR", tmp_path)
    monkeypatch.setattr(pa, "fetch_company_names", lambda *_args: ([], ""))
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    monkeypatch.setattr(mcp_server, "_find", lambda *_args: {"name": "synthetic"})
    monkeypatch.setattr(mcp_server.probes, "pg_target_of", lambda *_args: pa.PgTarget())
    monkeypatch.setattr(mcp_server, "_pinned_target", None)
    monkeypatch.setattr(mcp_server, "_host_filter", None)
    result = mcp_server.password_hashes("synthetic", "synthetic")
    count = 2 if legacy else 0
    assert f"({count} hashes, mode 12100)" in result
    path = Path(result.split(" (", 1)[0])
    records = [line for line in path.read_text().splitlines() if line]
    assert len(records) == count
    assert json.loads(path.with_suffix(".loginmap.json").read_text()) == (
        {"1": "modern", "2": "legacy"} if legacy else {}
    )


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
