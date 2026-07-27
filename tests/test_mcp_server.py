from odoo_activity import mcp_server


def test_mcp_tools_do_not_crash():
    """Smoke test: each tool runs end-to-end (real probes, no mocking) and
    returns the expected shape — a regression guard against future changes
    to probes.py breaking the MCP wrappers, not a check of probe values."""
    instances = mcp_server.list_instances()
    assert isinstance(instances, list)

    if instances:
        name = instances[0]["name"]
        assert mcp_server.get_instance(name) is not None
        assert isinstance(mcp_server.list_processes(name), list)

    assert mcp_server.get_instance("__no_such_instance__") is None
    assert mcp_server.list_processes("__no_such_instance__") == []
