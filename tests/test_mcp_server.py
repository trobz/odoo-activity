import inspect
import re

from odoo_activity import mcp_server


def test_host_filter_fullmatch_not_prefix(monkeypatch):
    monkeypatch.setattr(mcp_server, "_host_filter", re.compile("prod"))
    assert mcp_server._allowed("prod") is True
    assert mcp_server._allowed("prod-evil.example.com") is False
    assert mcp_server._allowed(None) is True  # local always allowed


def test_ssh_config_aliases_skips_wildcards_and_negation(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host demo\nHost *.internal\nHost !skip real\n")
    assert mcp_server._ssh_config_aliases(cfg) == ["demo", "real"]


def test_db_query_has_no_include_sensitive_information_argument():
    """No tool call can ask for plaintext -- the parameter must not exist on
    db_query's callable signature (what FastMCP turns into the tool schema
    an agent sees), only on the launch-time CLI flag."""
    assert "include_sensitive_information" not in inspect.signature(mcp_server.db_query).parameters


def test_db_query_honors_launch_time_flag_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **kw: captured.update(kw) or None)

    monkeypatch.setattr(mcp_server, "_include_sensitive_information", False)
    mcp_server.db_query("demo", "params")
    assert captured["include_sensitive_information"] is False

    monkeypatch.setattr(mcp_server, "_include_sensitive_information", True)
    mcp_server.db_query("demo", "params")
    assert captured["include_sensitive_information"] is True


def test_mcp_tools_do_not_crash():
    """Smoke test: each tool runs end-to-end (real probes, no mocking) and
    returns the expected shape — a regression guard against future changes
    to probes.py breaking the MCP wrappers, not a check of probe values."""
    instances = mcp_server.list_instances()
    assert isinstance(instances, list)

    if instances:
        name = instances[0]["name"]
        assert mcp_server.get_instance(name) is not None
        assert isinstance(mcp_server.list_top(name), list)
        assert isinstance(mcp_server.instance_databases(name), dict)
        assert isinstance(mcp_server.instance_top(name), dict)
        assert isinstance(mcp_server.instance_config(name), str)
        assert isinstance(mcp_server.instance_log_tail(name), str)

    stats = mcp_server.host_stats()
    assert stats is None or isinstance(stats, dict)

    assert mcp_server.get_instance("__no_such_instance__") is None
    assert mcp_server.list_top("__no_such_instance__") == []
    # not exercised against a real instance: SIGQUIT is a live signal, not a
    # read — only the not-found path is safe to smoke-test unconditionally.
    assert mcp_server.instance_dump_stacks("__no_such_instance__") == {"error": "(no such instance)", "workers": []}
