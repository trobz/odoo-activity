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
