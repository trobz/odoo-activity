"""Configuration inventory: real SQL filtering and MCP opt-in/routing."""

import sqlite3

import pytest

from odoo_activity import mcp_server, plugins
from odoo_activity.plugins import config_audit as ca
from odoo_activity.probes import LOCAL, PgTarget


def test_inventory_filters_modules_and_secrets_preserving_unset_and_blank(monkeypatch):
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE ir_module_module (name, latest_version, auto_install, state);
            INSERT INTO ir_module_module VALUES
                ('base', '16.0.1.0', 'f', 'installed'),
                ('auto', NULL, 't', 'installed'),
                ('pending_upgrade', '16.0.1.1', 'f', 'to upgrade'),
                ('pending_removal', '16.0.1.0', 'f', 'to remove'),
                ('pending_install', '16.0', 'f', 'to install'),
                ('unused', '16.0', 'f', 'uninstalled');
            CREATE TABLE ir_config_parameter (key, value);
            INSERT INTO ir_config_parameter VALUES
                ('auth_signup.invitation_scope', 'b2b'),
                ('auth_password_policy.minlength', '12'),
                ('web.base.url', ''),
                ('database.is_shrunk', 'True'),
                ('database.source_dump_datetime', '20260519T143000'),
                ('database.secret', 'do-not-export');
        """)
        monkeypatch.setattr(ca, "_psql_csv", lambda sql, *_: (list(db.execute(sql)), ""))
        result = ca.retrieve("test", PgTarget())
    assert result["modules"] == [
        {"name": "auto", "version": "", "auto_install": True, "state": "installed"},
        {"name": "base", "version": "16.0.1.0", "auto_install": False, "state": "installed"},
        {"name": "pending_removal", "version": "16.0.1.0", "auto_install": False, "state": "to remove"},
        {"name": "pending_upgrade", "version": "16.0.1.1", "auto_install": False, "state": "to upgrade"},
    ]
    params = {p["key"]: p["value"] for p in result["config_parameters"]}
    assert params["auth_signup.invitation_scope"] == "b2b"
    assert params["auth_password_policy.minlength"] == "12"  # noqa: S105 — policy value, not a password
    assert params["web.base.url"] == ""
    assert params["auth_totp.policy"] is None
    assert params["database.is_shrunk"] == "True"
    assert params["database.source_dump_datetime"] == "20260519T143000"
    assert params["database.is_anonymized"] is None
    assert "database.secret" not in params
    assert "do-not-export" not in str(result)


@pytest.mark.parametrize("failure_call", [1, 2])
def test_query_errors_do_not_return_partial_audit(monkeypatch, failure_call):
    calls = []

    def query(*args):
        calls.append(args)
        return (None, "permission denied") if len(calls) == failure_call else ([], "")

    monkeypatch.setattr(ca, "_psql_csv", query)
    with pytest.raises(ValueError, match="permission denied"):
        ca.retrieve("db", PgTarget())


def test_plugin_is_discovered_but_opt_in():
    installed, _ = plugins.load()
    assert any(p.name == "config-audit" for p in installed)
    assert "config-audit" not in [p.name for p in plugins.select(installed, [], [])]
    assert [p.name for p in plugins.select(installed, ["config-audit"], [])] == ["config-audit"]


def test_mcp_gate_and_instance_connection(monkeypatch):
    monkeypatch.setattr(mcp_server, "_resolve_host", lambda *_: LOCAL)
    monkeypatch.setattr(mcp_server, "_enabled_plugins", set())
    with pytest.raises(ValueError, match="config-audit disabled"):
        mcp_server.config_audit("instance", "db")
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"config-audit"})
    monkeypatch.setattr(mcp_server, "_find", lambda *_: None)
    with pytest.raises(ValueError, match="no such instance"):
        mcp_server.config_audit("instance", "db")
    inst = {"name": "instance"}
    pg = PgTarget()
    monkeypatch.setattr(mcp_server, "_find", lambda *_: inst)

    def connection(actual_inst, host):
        assert actual_inst is inst and host is LOCAL
        return pg

    def retrieve(database, actual_pg, host):
        assert database == "db" and actual_pg is pg and host is LOCAL
        return {"database": database, "modules": [], "config_parameters": []}

    monkeypatch.setattr(mcp_server.probes, "pg_target_of", connection)
    monkeypatch.setattr(ca, "retrieve", retrieve)
    assert mcp_server.config_audit("instance", "db")["instance"] == "instance"


_TABLES = """
    CREATE TABLE ir_module_module (name, latest_version, auto_install, state);
    CREATE TABLE ir_config_parameter (key, value);
"""


def _fake_db(script: str, columns: list[str]):
    """sqlite stands in for PostgreSQL; pg_attribute answers with `columns`."""
    db = sqlite3.connect(":memory:")
    db.executescript(_TABLES)
    db.executescript(script)
    calls = []

    def query(sql, *_):
        calls.append(sql)
        if "pg_catalog.pg_attribute" in sql:
            return [[c] for c in columns], ""
        # psql CSV: booleans as t/f, every cell a string or NULL
        return [[None if v is None else str(v) for v in row] for row in db.execute(sql)], ""

    return db, query, calls


def test_password_security_per_company_before_17(monkeypatch):
    db, query, _ = _fake_db(
        """
        INSERT INTO ir_module_module VALUES ('password_security', '15.0.1.0', 'f', 'installed');
        CREATE TABLE res_company (id, name, password_policy_enabled, password_length, password_lower,
                                  password_upper, password_numeric, password_special, password_estimate,
                                  password_expiration, password_minimum, password_history, secret);
        INSERT INTO res_company VALUES (1, 'Main', 't', 12, 1, 1, 1, 0, 3, 60, 24, 30, 'nope'),
                                       (2, 'Other', 'f', 8, 0, 0, 0, 0, 0, 0, 0, NULL, 'nope');
        """,
        [
            "password_history",
            "password_length",
            "password_policy_enabled",
            "password_lower",
            "password_upper",
            "password_numeric",
            "password_special",
            "password_estimate",
            "password_expiration",
            "password_minimum",
        ],
    )
    monkeypatch.setattr(ca, "_psql_csv", query)
    with db:
        result = ca.retrieve("test", PgTarget())
    main, other = result["password_security"]["companies"]
    assert main == {
        "id": 1,
        "name": "Main",
        "password_policy_enabled": True,
        "password_length": 12,
        "password_lower": 1,
        "password_upper": 1,
        "password_numeric": 1,
        "password_special": 0,
        "password_estimate": 3,
        "password_expiration": 60,
        "password_minimum": 24,
        "password_history": 30,
    }
    assert other["password_policy_enabled"] is False and other["password_history"] is None
    assert "password_length" in result["password_security"]["explanations"]
    assert "nope" not in str(result)


def test_password_security_16_reads_only_present_columns(monkeypatch):
    db, query, _ = _fake_db(
        """
        INSERT INTO ir_module_module VALUES ('password_security', '16.0.1.0', 'f', 'installed');
        CREATE TABLE res_company (id, name, password_expiration, password_history);
        INSERT INTO res_company VALUES (1, 'Main', 0, 5);
        """,
        ["password_expiration", "password_history"],
    )
    monkeypatch.setattr(ca, "_psql_csv", query)
    with db:
        result = ca.retrieve("test", PgTarget())
    assert result["password_security"]["companies"] == [
        {"id": 1, "name": "Main", "password_expiration": 0, "password_history": 5}
    ]
    assert set(result["password_security"]["explanations"]) == {"password_expiration", "password_history"}


def test_password_security_17_in_config_parameters(monkeypatch):
    db, query, _ = _fake_db(
        """
        INSERT INTO ir_module_module VALUES ('password_security', '17.0.1.0', 'f', 'installed');
        INSERT INTO ir_config_parameter VALUES ('password_security.expiration_days', '90'),
                                               ('password_security.special', '0');
        """,
        [],
    )
    monkeypatch.setattr(ca, "_psql_csv", query)
    with db:
        result = ca.retrieve("test", PgTarget())
    assert result["password_security"] == {"companies": [], "explanations": {}}
    params = {p["key"]: p["value"] for p in result["config_parameters"]}
    assert params["password_security.expiration_days"] == "90"  # noqa: S105 — policy value, not a password
    assert params["password_security.special"] == "0"  # noqa: S105 — policy value, not a password
    assert params["password_security.history"] is None


def test_password_security_absent_runs_no_company_query(monkeypatch):
    db, query, calls = _fake_db("INSERT INTO ir_module_module VALUES ('base', '16.0', 'f', 'installed');", [])
    monkeypatch.setattr(ca, "_psql_csv", query)
    with db:
        result = ca.retrieve("test", PgTarget())
    assert result["password_security"] is None
    assert len(calls) == 2


def test_password_security_query_error_fails_the_audit(monkeypatch):
    def query(sql, *_):
        if "ir_module_module" in sql:
            return [["password_security", "16.0.1.0", "f", "installed"]], ""
        if "pg_catalog.pg_attribute" in sql:
            return None, "permission denied for res_company"
        return [], ""

    monkeypatch.setattr(ca, "_psql_csv", query)
    with pytest.raises(ValueError, match="permission denied for res_company"):
        ca.retrieve("db", PgTarget())


def test_websites_reports_signup_scope_per_website(monkeypatch):
    db, query, _ = _fake_db(
        """
        INSERT INTO ir_module_module VALUES ('website', '17.0.1.0', 'f', 'installed');
        CREATE TABLE website (id, name, domain, auth_signup_uninvited, secret);
        INSERT INTO website VALUES (1, 'Main', 'https://example.com', 'b2b', 'nope'),
                                   (2, 'Shop', NULL, 'b2c', 'nope'),
                                   (3, 'Legacy', '', NULL, 'nope');
        """,
        ["domain", "auth_signup_uninvited"],
    )
    monkeypatch.setattr(ca, "_psql_csv", query)
    with db:
        result = ca.retrieve("test", PgTarget())
    assert result["websites"] == [
        {"id": 1, "name": "Main", "domain": "https://example.com", "auth_signup_uninvited": "b2b"},
        {"id": 2, "name": "Shop", "domain": None, "auth_signup_uninvited": "b2c"},
        {"id": 3, "name": "Legacy", "domain": "", "auth_signup_uninvited": None},
    ]
    assert "nope" not in str(result)


def test_websites_without_signup_column_returns_empty(monkeypatch):
    db, query, calls = _fake_db(
        "INSERT INTO ir_module_module VALUES ('website', '11.0.1.0', 'f', 'installed');",
        ["domain"],
    )
    monkeypatch.setattr(ca, "_psql_csv", query)
    with db:
        result = ca.retrieve("test", PgTarget())
    assert result["websites"] == []
    assert len(calls) == 3  # modules, parameters, pg_attribute only


def test_websites_absent_runs_no_website_query(monkeypatch):
    db, query, calls = _fake_db("INSERT INTO ir_module_module VALUES ('base', '17.0', 'f', 'installed');", [])
    monkeypatch.setattr(ca, "_psql_csv", query)
    with db:
        result = ca.retrieve("test", PgTarget())
    assert result["websites"] is None
    assert len(calls) == 2


def test_websites_query_error_fails_the_audit(monkeypatch):
    def query(sql, *_):
        if "ir_module_module" in sql:
            return [["website", "17.0.1.0", "f", "installed"]], ""
        if "pg_catalog.pg_attribute" in sql:
            return None, "permission denied for website"
        return [], ""

    monkeypatch.setattr(ca, "_psql_csv", query)
    with pytest.raises(ValueError, match="permission denied for website"):
        ca.retrieve("db", PgTarget())
