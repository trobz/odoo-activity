"""users-audit: PostgreSQL-8-compatible export, summary parity, batch and
credential-file discipline — all with synthetic CSV, never a live database."""

import base64
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from odoo_activity import mcp_server, probes
from odoo_activity.host import Host
from odoo_activity.plugins import password_audit as pa
from odoo_activity.plugins import users_audit as ua
from odoo_activity.probes import PgTarget, _psql_csv


def _hash(plain="synthetic"):
    salt = b"users-audit-salt"
    digest = hashlib.pbkdf2_hmac("sha512", plain.encode(), salt, 1)

    def ab64(raw):
        return base64.b64encode(raw).decode().rstrip("=").replace("+", ".")

    return f"$pbkdf2-sha512$1${ab64(salt)}${ab64(digest)}"


# probe row: has_crypt, has_share, has_totp, has_log, has_passkey, policy, version
_MODERN = ["0", "1", "1", "1", "0", None, "16.0.1.3"]
_ODOO8 = ["0", "0", "0", "0", "0", None, "8.0"]


def _fake_db(monkeypatch, *, probe=_MODERN, accounts=(), companies=("Acme",)):
    calls = []

    def fake_psql(sql, db, params, port, host):
        calls.append(sql)
        if "AS has_password_crypt" in sql:
            return [list(probe)], ""
        if "FROM res_users ru" in sql:
            return [list(row) for row in accounts], ""
        if "FROM res_company" in sql:
            return [[name] for name in companies], ""
        return [], ""

    monkeypatch.setattr(ua, "_psql_csv", fake_psql)
    return calls


# ---------------------------------------------------------------------------
# summary parity with odoo-db
# ---------------------------------------------------------------------------


def test_summarize_matches_odoo_db_shape():
    rows = [
        {"last_login": "2024-01-02T00:00:00", "mfa": "totp, passkey"},
        {"last_login": "2024-06-01T00:00:00", "mfa": "none"},
        {"last_login": None, "mfa": "mail"},
        {"last_login": "2023-12-31T23:59:59", "mfa": "none"},
    ]
    summary = ua.summarize_users(rows, by="year")
    assert summary == {
        "last_login_by_year": {"2023": 1, "2024": 2, "never": 1},
        "mfa_by_type": {"totp": 1, "passkey": 1, "mail": 1, "none": 2},
    }
    # `never` sorts last, and every factor is counted independently.
    assert list(summary["last_login_by_year"]) == ["2023", "2024", "never"]


@pytest.mark.parametrize(
    "totp_set,passkeys,share,policy,expected",
    [
        (True, 1, False, "employee_required", "totp, passkey"),
        (False, 1, False, "employee_required", "passkey, mail"),
        (False, 0, True, "employee_required", "none"),
        (False, 0, False, "all_required", "mail"),
        (False, 0, False, None, "none"),
    ],
)
def test_compute_mfa_matches_odoo_db(totp_set, passkeys, share, policy, expected):
    assert ua.compute_mfa(totp_set=totp_set, passkey_count=passkeys, share=share, totp_policy=policy) == expected


def test_payload_shaping_keeps_null_password_and_short_version():
    rows = [
        {
            "id": 2,
            "login": "admin",
            "email": "admin@example.test",
            "password": "$hash",
            "profile": "admin",
            "last_login": "2024-01-01",
            "mfa": "none",
        },
        {
            "id": 3,
            "login": "oauth",
            "email": "",
            "password": None,
            "profile": "internal",
            "last_login": None,
            "mfa": "none",
        },
    ]
    payload = ua.build_payload(
        database="db",
        instance="inst",
        host_identity_value="local",
        rows=rows,
        companies=["Acme"],
        odoo_version=ua._short_version("16.0.1.3"),
        include_inactive=False,
    )
    assert payload["odoo_version"] == "16.0"
    assert payload["include_inactive"] is False
    assert payload["users"] == [
        {"id": 2, "login": "admin", "email": "admin@example.test", "password": "$hash", "profile": "admin"},
        {"id": 3, "login": "oauth", "email": "", "password": None, "profile": "internal"},
    ]
    assert payload["exported_at"].endswith("+00:00")


# ---------------------------------------------------------------------------
# PostgreSQL compatibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("log", [False, True])
@pytest.mark.parametrize("share", [False, True])
def test_accounts_sql_uses_no_json_functions_or_to_regclass(legacy, log, share):
    sql = ua._accounts_sql(legacy=legacy, log=log, totp=True, passkey=True, share=share, include_inactive=False)
    for banned in ("json_agg", "json_build_object", "to_regclass", "row_to_json", "::json"):
        assert banned not in sql
    if legacy:
        assert "password_crypt" in sql
    else:
        assert "password_crypt" not in sql
    assert ("ru.login_date" in sql) is (not log)
    assert ("FALSE AS share" not in sql) is share


def test_psql_csv_parses_null_empty_and_embedded_separators(monkeypatch):
    stdout = '2,admin,"a,b@example.test","line1\nline2",\\N,t,0,f\n3,x,"say ""hi""",,\\N,f,2,t\n'
    monkeypatch.setattr(Host, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""))
    rows, error = _psql_csv("SELECT 1", "db", {}, PgTarget(), Host())
    assert error == ""
    assert rows == [
        ["2", "admin", "a,b@example.test", "line1\nline2", None, "t", "0", "f"],
        ["3", "x", 'say "hi"', "", None, "f", "2", "t"],
    ]


def test_psql_csv_preserves_psql_errors(monkeypatch):
    monkeypatch.setattr(
        Host, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
    )
    assert _psql_csv("SELECT 1", "db", {}, PgTarget(), Host()) == (None, "permission denied")


def test_accounts_sql_selects_login_date_and_no_share_on_odoo8():
    sql = ua._accounts_sql(legacy=False, log=False, totp=False, passkey=False, share=False, include_inactive=False)
    assert "ru.login_date" in sql
    assert "FALSE AS share" in sql
    assert "LEFT JOIN (SELECT create_uid AS uid, MAX(create_date)" not in sql


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_database_writes_a_private_accounts_file(monkeypatch, tmp_path):
    accounts = [
        ["2", "admin", "admin@example.test", "$hash", "2024-05-01 10:00:00", "t", "0", "f", "admin"],
        ["3", "portal", "", "", None, "f", "0", "t", "portal"],
    ]
    _fake_db(monkeypatch, accounts=accounts)
    run = tmp_path / "run"
    run.mkdir()

    entry, error = ua.export_database(
        instance="acme",
        database="acme_prod",
        pg=PgTarget(),
        host=Host(),
        host_identity_value="local",
        run=run,
    )

    assert error == ""
    assert entry is not None
    path = Path(entry["path"])
    assert path.name == "acme_prod_accounts.json"
    assert path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text())
    assert payload["database"] == "acme_prod"
    assert payload["instance"] == "acme"
    assert payload["host"] == "local"
    assert payload["odoo_version"] == "16.0"
    assert payload["companies"] == ["Acme"]
    assert payload["summary"] == {
        "last_login_by_year": {"2024": 1, "never": 1},
        "mfa_by_type": {"totp": 1, "none": 1},
    }
    assert payload["users"] == [
        {"id": 2, "login": "admin", "email": "admin@example.test", "password": "$hash", "profile": "admin"},
        {"id": 3, "login": "portal", "email": "", "password": "", "profile": "portal"},
    ]
    assert entry["users"] == 2


def test_export_database_handles_odoo8_shape(monkeypatch, tmp_path):
    _fake_db(
        monkeypatch,
        probe=_ODOO8,
        accounts=[["5", "old", "old@example.test", "$hash", "2015-01-02 03:04:05", "f", "0", "f", "internal"]],
    )
    run = tmp_path / "run"
    run.mkdir()
    entry, error = ua.export_database(instance="legacy", database="db8", pg=PgTarget(), host=Host(), run=run)
    assert error == "" and entry is not None
    payload = json.loads(Path(entry["path"]).read_text())
    assert payload["summary"]["mfa_by_type"] == {"none": 1}
    assert payload["summary"]["last_login_by_year"] == {"2015": 1}
    assert payload["odoo_version"] == "8.0"


def test_export_database_preserves_a_psql_error(monkeypatch):
    def failing(sql, db, params, port, host):
        return None, "permission denied for res_users"

    monkeypatch.setattr(ua, "_psql_csv", failing)
    entry, error = ua.export_database(instance="x", database="y", pg=PgTarget(), host=Host())
    assert entry is None
    assert error == "permission denied for res_users"


# ---------------------------------------------------------------------------
# batch tool: partial failure, disclosure, gates, host rules
# ---------------------------------------------------------------------------


def _open_gate(monkeypatch, *, entry=None, error=""):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"users-audit"})
    monkeypatch.setattr(mcp_server, "_users_audit", lambda: ua)
    monkeypatch.setattr(ua, "run_path", lambda root=None: Path(root) if root else Path("/tmp/run"))
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "inst"})
    monkeypatch.setattr(mcp_server.probes, "pg_target_of", lambda *_a: PgTarget())
    if entry is not None or error:
        monkeypatch.setattr(ua, "export_database", lambda **_kw: (entry, error))


def test_batch_reports_partial_failure_without_disclosing_accounts(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"users-audit"})
    monkeypatch.setattr(mcp_server, "_users_audit", lambda: ua)
    monkeypatch.setattr(ua, "run_path", lambda root=None: tmp_path)
    monkeypatch.setattr(mcp_server, "_resolve_host", lambda *_a: Host())
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "inst"})
    monkeypatch.setattr(mcp_server.probes, "pg_target_of", lambda *_a: PgTarget())

    def export(**kwargs):
        if kwargs["database"] == "bad":
            return None, "permission denied"
        return {
            "host": "local",
            "instance": kwargs["instance"],
            "database": kwargs["database"],
            "path": str(tmp_path / "ok_accounts.json"),
            "users": 1,
            "summary": {"last_login_by_year": {"never": 1}, "mfa_by_type": {"none": 1}},
            "error": None,
        }, ""

    monkeypatch.setattr(ua, "export_database", export)
    result = mcp_server.users_audit([{"instance": "inst", "db": "ok"}, {"instance": "inst", "db": "bad"}])
    manifest = json.loads(result)
    assert [d["error"] for d in manifest["databases"]] == [None, "permission denied"]
    assert manifest["databases"][0]["users"] == 1
    for secret in ("admin", "password", "$pbkdf2"):
        assert secret not in result


def test_users_audit_is_gated(monkeypatch):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", set())
    with pytest.raises(ValueError, match="users-audit disabled"):
        mcp_server.users_audit([{"instance": "x", "db": "y"}])


def test_users_audit_rejects_malformed_targets(monkeypatch, tmp_path):
    _open_gate(
        monkeypatch,
        entry={
            "host": "local",
            "instance": "i",
            "database": "d",
            "path": "p",
            "users": 0,
            "summary": None,
            "error": None,
        },
    )
    monkeypatch.setattr(ua, "run_path", lambda root=None: tmp_path)
    manifest = json.loads(mcp_server.users_audit(["not-an-object", {"instance": "x"}]))
    assert [d["error"] for d in manifest["databases"]] == [
        "target must be an object",
        "target needs a `db` name",
    ]


def test_users_audit_honours_pinned_and_filtered_hosts(monkeypatch, tmp_path):
    _open_gate(
        monkeypatch,
        entry={"host": "h", "instance": "i", "database": "d", "path": "p", "users": 0, "summary": None, "error": None},
    )
    monkeypatch.setattr(ua, "run_path", lambda root=None: tmp_path)

    # multi: a host outside --host-filter is refused per target
    monkeypatch.setattr(mcp_server, "_pinned_target", None)
    monkeypatch.setattr(mcp_server, "_host_filter", re.compile("prod-.*"))
    manifest = json.loads(mcp_server.users_audit([{"host": "evil", "instance": "i", "db": "d"}]))
    assert "rejected by --host-filter" in manifest["databases"][0]["error"]

    # pinned: a different host is refused, the pinned one works
    monkeypatch.setattr(mcp_server, "_pinned_target", Host(alias="prod-1"))
    manifest = json.loads(mcp_server.users_audit([{"host": "prod-2", "instance": "i", "db": "d"}]))
    assert "pinned to" in manifest["databases"][0]["error"]
    manifest = json.loads(mcp_server.users_audit([{"instance": "i", "db": "d"}]))
    assert manifest["databases"][0]["error"] is None


# ---------------------------------------------------------------------------
# password-audit consumes the accounts file, never postgres
# ---------------------------------------------------------------------------


def test_password_hashes_converts_an_accounts_file_without_psql(monkeypatch, tmp_path):
    monkeypatch.setattr(pa, "WORK_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    accounts = {
        "database": "acme_prod",
        "instance": "acme",
        "host": "local",
        "companies": ["Acme"],
        "users": [
            {"id": 1, "login": "admin", "email": "", "password": _hash("admin")},
            {"id": 2, "login": "oauth", "email": "", "password": None},
            {"id": 3, "login": "plain", "email": "", "password": "plaintext"},
        ],
    }
    path = tmp_path / "acme_prod_accounts.json"
    path.write_text(json.dumps(accounts))

    def no_psql(*_a, **_k):
        msg = "password_hashes must not call psql"
        raise AssertionError(msg)

    monkeypatch.setattr(probes.Host, "run", no_psql)

    result = mcp_server.password_hashes(str(path))
    assert "(1 hashes, mode 12100)" in result
    hash_file = Path(result.split(" (", 1)[0])
    assert len([line for line in hash_file.read_text().splitlines() if line]) == 1
    assert json.loads(hash_file.with_suffix(".loginmap.json").read_text()) == {"1": "admin"}
    assert "Acme" in hash_file.with_suffix(".derived.txt").read_text()


def test_password_hashes_rejects_a_non_accounts_file(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    path = tmp_path / "not_accounts.json"
    path.write_text(json.dumps({"foo": 1}))
    with pytest.raises(ValueError, match="not a users-audit accounts file"):
        mcp_server.password_hashes(str(path))


# ---------------------------------------------------------------------------
# CWD pollution
# ---------------------------------------------------------------------------


def test_cwd_cannot_supply_or_divert_accounts_files(monkeypatch, tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "evil_accounts.json").write_text("{}")
    out = tmp_path / "out"
    monkeypatch.setattr(ua, "WORK_DIR", out)
    monkeypatch.chdir(cwd)

    run = ua.run_path()
    assert run.parent == out
    assert not any(p.name.endswith("_accounts.json") for p in run.rglob("*"))
    path = ua.accounts_path(run, "local", "inst", "db")
    assert str(path).startswith(str(run))


@pytest.mark.parametrize("targets", [None, {}, "targets", (), [], [{}] * 101])
def test_batch_rejects_invalid_list_before_creating_run(monkeypatch, targets):
    _open_gate(monkeypatch)
    monkeypatch.setattr(ua, "run_path", lambda *_: pytest.fail("must validate before creating run"))
    with pytest.raises(ValueError):
        mcp_server.users_audit(targets)


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_batch_requires_boolean_include_inactive(monkeypatch, value):
    _open_gate(monkeypatch)
    with pytest.raises(ValueError, match="boolean"):
        mcp_server.users_audit([{"instance": "i", "db": "d"}], include_inactive=value)


@pytest.mark.parametrize("value", ["", " ", "\0", 1, [], {}])
def test_batch_rejects_invalid_output_dir(monkeypatch, value):
    _open_gate(monkeypatch)
    with pytest.raises(ValueError, match="output_dir"):
        mcp_server.users_audit([{"instance": "i", "db": "d"}], output_dir=value)


@pytest.mark.parametrize(
    "spec",
    [
        None,
        [],
        1,
        {},
        {"db": "d"},
        {"instance": "i"},
        {"instance": " ", "db": "d"},
        {"instance": "i", "db": "\0"},
        {"instance": {"login": "SECRET"}, "db": object()},
        {"instance": "i", "db": "d", "host": {"password": "SECRET"}},
        {"instance": "i", "db": "d", "host": ""},
        {"instance": "i", "db": "d", "host": "-oProxyCommand=command"},
        *[{"instance": "i", "db": "d", "ssh_port": port} for port in [True, False, "22", 22.0, 0, -1, 65536]],
    ],
)
def test_invalid_target_returns_serializable_error_without_raw_values(monkeypatch, spec):
    _open_gate(monkeypatch)
    monkeypatch.setattr(mcp_server, "_find", lambda *_: pytest.fail("invalid target reached discovery"))
    result = mcp_server.users_audit([spec])
    assert json.loads(result)["databases"][0]["error"]
    assert "SECRET" not in result


def test_duplicate_resolved_targets_are_not_exported_twice(monkeypatch, tmp_path):
    _open_gate(monkeypatch)
    monkeypatch.setattr(mcp_server, "_pinned_target", Host(alias="pinned"))
    calls = []

    def export(**kwargs):
        calls.append(kwargs)
        return {"error": None}, ""

    monkeypatch.setattr(ua, "export_database", export)
    result = json.loads(
        mcp_server.users_audit([
            {"instance": "i", "db": "d"},
            {"instance": "i", "db": "d", "host": "pinned"},
            {"instance": "i", "db": "other"},
        ])
    )
    assert len(calls) == 2
    assert result["databases"][1]["error"] == "duplicate target"


@pytest.mark.parametrize("failure", ["stderr", "decode", "directory", "discovery"])
def test_target_failure_is_private_and_does_not_abort_batch(monkeypatch, failure):
    _open_gate(monkeypatch)
    secret = "private-login private@example.test $pbkdf2-secret"  # noqa: S105 — synthetic disclosure marker

    def export(**kwargs):
        if kwargs["database"] == "bad":
            if failure == "stderr":
                return None, secret
            if failure == "directory":
                raise PermissionError(secret)
            raise ValueError(secret)
        return {"error": None}, ""

    def find(instance, *_):
        if instance == "bad" and failure == "discovery":
            raise OSError(secret)
        return {"name": instance}

    monkeypatch.setattr(ua, "export_database", export)
    monkeypatch.setattr(mcp_server, "_find", find)
    result = mcp_server.users_audit([{"instance": "bad", "db": "bad"}, {"instance": "ok", "db": "ok"}])
    assert secret not in result
    assert [bool(row["error"]) for row in json.loads(result)["databases"]] == [True, False]


@pytest.mark.parametrize("kind", ["file", "symlink", "public"])
def test_output_dir_errors_are_argument_errors(monkeypatch, tmp_path, kind):
    monkeypatch.setattr(mcp_server, "_users_audit", lambda: ua)
    root = tmp_path / "out"
    if kind == "file":
        root.write_text("")
    elif kind == "symlink":
        root.symlink_to(tmp_path, target_is_directory=True)
    else:
        root.mkdir(mode=0o755)
        root.chmod(0o755)
    with pytest.raises(ValueError, match=r"private.*run directory"):
        mcp_server.users_audit([{"instance": "i", "db": "d"}], output_dir=str(root))


def test_run_paths_are_absolute_fresh_and_private(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    first = ua.run_path(Path("nested/output"))
    second = ua.run_path(Path("nested/output"))
    assert first.is_absolute() and first != second
    assert first.stat().st_mode & 0o777 == 0o700


def test_slugs_do_not_collide_or_escape_run(tmp_path):
    run = ua.run_path(tmp_path)
    values = ["../db", "__db", ".", "..", "database", "a/b", "a_b", "a" * 101, "a" * 102]
    paths = [ua.accounts_path(run, value, value, value) for value in values]
    assert len(set(paths)) == len(values)
    assert all(path.is_relative_to(run) for path in paths)
    assert ua.accounts_path(run, "local", "inst", "db").name == "db_accounts.json"


def test_manifest_summary_does_not_echo_malformed_account_dates(monkeypatch, tmp_path):
    secret = "private@example.test"  # noqa: S105 — synthetic disclosure marker
    _fake_db(monkeypatch, accounts=[["1", "private-login", secret, "$secret-hash", secret, "f", "0", "f", "internal"]])
    entry, error = ua.export_database(instance="i", database="d", pg=PgTarget(), run=tmp_path)
    assert not error
    result = json.dumps(entry)
    assert all(value not in result for value in [secret, "private-login", "$secret-hash"])


@pytest.mark.parametrize("value", [None, 1, [], {}, "", " ", "\0"])
def test_password_hashes_invalid_path_values(monkeypatch, value):
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    with pytest.raises(ValueError, match="path string"):
        mcp_server.password_hashes(value)


@pytest.mark.parametrize("content", [b"{", b"\xff", b"null", b"[]", b"{}", b'{"users":null}', b'{"users":{}}'])
def test_password_hashes_invalid_json(monkeypatch, tmp_path, content):
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    path = tmp_path / "accounts.json"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="not a users-audit accounts file"):
        mcp_server.password_hashes(str(path))


@pytest.mark.parametrize(
    "row",
    [
        None,
        [],
        "SECRET",
        {},
        {"id": 1, "login": "SECRET"},
        *[{"id": value, "login": "SECRET", "password": None} for value in [None, True, "1", 1.0, [], {}, 0]],
        *[{"id": 1, "login": value, "password": None} for value in [None, 1, [], {}, ""]],
        *[{"id": 1, "login": "SECRET", "password": value} for value in [False, 1, [], {}]],
    ],
)
def test_password_hashes_rejects_bad_rows_before_writing(monkeypatch, tmp_path, row):
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    monkeypatch.setattr(pa, "export_path", lambda *_: pytest.fail("invalid data reserved an export"))
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"users": [row]}))
    with pytest.raises(ValueError, match="row at index 0") as exc:
        mcp_server.password_hashes(str(path))
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize("companies", [None, "SECRET", {}, [1], [{"name": "SECRET"}]])
def test_password_hashes_rejects_bad_companies(monkeypatch, tmp_path, companies):
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"users": [], "companies": companies}))
    with pytest.raises(ValueError, match="list of strings"):
        mcp_server.password_hashes(str(path))


def test_password_hashes_rejects_duplicate_user_ids():
    row = {"id": 1, "login": "private", "password": None}
    with pytest.raises(ValueError, match="duplicate accounts user id"):
        pa.rows_from_accounts({"users": [row, row]})


def test_password_hashes_missing_file_or_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    for path in [tmp_path, tmp_path / "missing"]:
        with pytest.raises(ValueError, match="unreadable"):
            mcp_server.password_hashes(str(path))


def test_password_hashes_expands_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pa, "WORK_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "_password_audit", lambda: pa)
    (tmp_path / "accounts.json").write_text(json.dumps({"users": []}))
    assert "(0 hashes" in mcp_server.password_hashes("~/accounts.json")


def _profile(groups: list[int], *, share: bool | None = None) -> str:
    """Run the account SQL against a minimal schema and return user 7's profile.

    `share=None` models Odoo 8 (no `share` column); otherwise the column is
    added with the given value. Memberships are explicit only, with arbitrary
    group IDs and unrelated XML IDs to guard against loose joins.
    """
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE res_users (id, login, partner_id, password, login_date, active);
            CREATE TABLE res_partner (id, email);
            CREATE TABLE res_groups_users_rel (uid, gid);
            CREATE TABLE res_groups_implied_rel (gid, hid);
            INSERT INTO res_groups_implied_rel VALUES
                (50, 30), (60, 61), (61, 10), (70, 71), (71, 70);
            CREATE TABLE ir_model_data (module, name, model, res_id);
            INSERT INTO res_users VALUES (7, 'user', 1, NULL, NULL, TRUE);
            INSERT INTO res_partner VALUES (1, 'user@example.test');
            INSERT INTO ir_model_data VALUES
                ('base', 'group_system', 'res.groups', 10),
                ('base', 'group_portal', 'res.groups', 20),
                ('base', 'group_user', 'res.groups', 30),
                ('base', 'group_erp_manager', 'res.groups', 40),
                ('custom', 'group_system', 'res.groups', 30),
                ('base', 'group_system', 'other.model', 30);
        """)
        if share is not None:
            db.execute("ALTER TABLE res_users ADD COLUMN share")
            db.execute("UPDATE res_users SET share = ?", (share,))
        # Another user's Settings membership must not affect this user's profile.
        db.execute("INSERT INTO res_groups_users_rel VALUES (8, 10)")
        db.executemany("INSERT INTO res_groups_users_rel VALUES (7, ?)", [(gid,) for gid in groups])
        sql = ua._accounts_sql(
            legacy=False, log=False, totp=False, passkey=False, share=share is not None, include_inactive=False
        )
        rows = [[str(cell) if cell is not None else None for cell in row] for row in db.execute(sql)]
    return ua._decode_rows(rows, policy=None)[0]["profile"]


_PROFILE_CASES = [
    ([], "portal"),
    ([20], "portal"),
    ([30], "internal"),
    ([30, 40], "internal"),
    ([10], "admin"),
    ([10, 30], "admin"),
    ([10, 20, 30], "admin"),
    ([50], "internal"),  # only an implied internal membership
    ([60], "admin"),  # multi-hop implied Settings membership
    ([70], "portal"),  # cycle with no internal/admin access
]


@pytest.mark.parametrize("groups,expected", _PROFILE_CASES)
def test_profile_from_effective_groups_without_share(groups, expected):
    # Odoo 8: internal access comes from direct or implied base.group_user.
    assert _profile(groups) == expected


@pytest.mark.parametrize("groups,expected", _PROFILE_CASES)
def test_profile_with_share_consistent_with_groups(groups, expected):
    # 9.0+: Odoo stores share = not has_group('base.group_user').
    assert _profile(groups, share=expected == "portal") == expected


@pytest.mark.parametrize(
    "groups,share,expected",
    [
        ([30], True, "portal"),  # direct group_user ignored: share decides
        ([50], True, "portal"),  # implied group_user ignored: share decides
        ([], False, "internal"),  # no groups at all: share decides
        ([20], False, "internal"),
        ([60], True, "admin"),  # Settings (even implied) still takes precedence
    ],
)
def test_profile_internal_access_follows_share_column(groups, share, expected):
    # Groups and share disagree on purpose, proving which source is used.
    assert _profile(groups, share=share) == expected
