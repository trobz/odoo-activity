import inspect
import json
import re
from pathlib import Path

import pytest

from odoo_activity import mcp_server
from odoo_activity.plugins import odooly as odooly_plugin
from odoo_activity.plugins import pos as pos_plugin


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


def test_instance_log_analysis_no_such_instance(monkeypatch):
    monkeypatch.setattr(mcp_server, "_find", lambda *_: None)
    assert mcp_server.instance_log_analysis("demo", "errors") == "(no such instance)"


def test_instance_log_analysis_no_log_file(monkeypatch):
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: [])
    assert mcp_server.instance_log_analysis("demo", "errors") == "(no log file found)"


def test_instance_log_analysis_runs_odoo_logs_against_resolved_files(monkeypatch):
    """The tool body is the same shape as db_query's -- resolve, run,
    parse -- just swapping instance_log_files/start_odoo_logs in."""
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: [Path("/var/log/server.log")])

    captured = {}

    class _FakeProc:
        def communicate(self, timeout=None):
            return json.dumps([{"type": "AccessError", "count": 3}]), ""

    def fake_start(command, files, host, *, since=None, until=None, database=None):
        captured["command"] = command
        captured["files"] = files
        return _FakeProc()

    monkeypatch.setattr(mcp_server.probes, "start_odoo_logs", fake_start)

    result = mcp_server.instance_log_analysis("demo", "errors")
    assert result == [{"type": "AccessError", "count": 3}]
    assert captured == {"command": "errors", "files": [Path("/var/log/server.log")]}


def _instance_with_logs(monkeypatch, files):
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: files)


class _FakeProc:
    def __init__(self, stdout="[]", stderr=""):
        self._result = (stdout, stderr)
        self.returncode = 0

    def communicate(self, timeout=None):
        return self._result


def test_instance_log_analysis_with_a_window_reads_only_the_overlapping_files(monkeypatch):
    """The window is resolved by `odoo-logs list` first, then handed to the
    analysis itself, so it neither parses the other files nor returns rows
    from outside the window."""
    everything = [Path("/var/log/server.log"), Path("/var/log/server.log.2026-09-25"), Path("/var/log/server.log.2")]
    _instance_with_logs(monkeypatch, everything)
    calls = []

    def fake_start(command, files, host, *, since=None, until=None, database=None):
        calls.append({"command": command, "files": files, "since": since, "until": until})
        if command == "list":
            return _FakeProc(json.dumps([{"path": "/var/log/server.log.2026-09-25"}]))
        return _FakeProc(json.dumps([{"type": "AccessError", "count": 3}]))

    monkeypatch.setattr(mcp_server.probes, "start_odoo_logs", fake_start)

    result = mcp_server.instance_log_analysis("demo", "errors", since="2026-09-25 19:50", until="2026-09-25 20:10")

    window = {"since": "2026-09-25 19:50", "until": "2026-09-25 20:10"}
    assert result == [{"type": "AccessError", "count": 3}]
    assert calls == [
        {"command": "list", "files": everything, **window},
        {"command": "errors", "files": [Path("/var/log/server.log.2026-09-25")], **window},
    ]


def test_instance_log_analysis_forwards_the_database(monkeypatch):
    """`-d` is an odoo-logs global option; the tool just hands it on, with or
    without a window."""
    _instance_with_logs(monkeypatch, [Path("/var/log/server.log")])
    seen = []

    def fake_start(command, files, host, *, since=None, until=None, database=None):
        seen.append((command, database))
        return _FakeProc(json.dumps([{"path": "/var/log/server.log"}]) if command == "list" else "[]")

    monkeypatch.setattr(mcp_server.probes, "start_odoo_logs", fake_start)

    mcp_server.instance_log_analysis("demo", "calls", database="lalouve_staging")
    mcp_server.instance_log_analysis("demo", "calls", since="2026-09-29", database="lalouve_staging")

    # the window pass (`list`) looks at files, not databases
    assert seen == [("calls", "lalouve_staging"), ("list", None), ("calls", "lalouve_staging")]


def test_instance_error_traceback_cuts_row_timestamps_to_the_second(monkeypatch):
    """A row's own `first`/`last` carry microseconds, which odoo-logs's
    --from/--to rejects ("unrecognized date: '2026-10-02 02:00:07.836000'"):
    they are cut to the second before being passed on."""
    _instance_with_logs(monkeypatch, [Path("/var/log/server.log")])
    captured = {}

    def fake_files_in_window(files, since, until, host):
        captured["since"] = since
        captured["until"] = until
        return files

    def fake_error_traceback(files, error_type, error, host, *, since=None, until=None, database=None):
        captured["tb_since"] = since
        return "Traceback text"

    monkeypatch.setattr(mcp_server.probes, "files_in_window", fake_files_in_window)
    monkeypatch.setattr(mcp_server.probes, "error_traceback", fake_error_traceback)

    result = mcp_server.instance_error_traceback(
        "demo", "AccessError", "(N)", since="2026-10-02 02:00:07.836000", until="2026-10-02 02:59:59,999"
    )
    assert result == "Traceback text"
    assert captured == {
        "since": "2026-10-02 02:00:07",
        "until": "2026-10-02 02:59:59",
        "tb_since": "2026-10-02 02:00:07",
    }


def test_instance_error_traceback_forwards_the_database(monkeypatch):
    _instance_with_logs(monkeypatch, [Path("/var/log/server.log")])
    captured = {}

    def fake_error_traceback(files, error_type, error, host, *, since=None, until=None, database=None):
        captured["database"] = database
        return "KeyError: 'socket'"

    monkeypatch.setattr(mcp_server.probes, "error_traceback", fake_error_traceback)

    mcp_server.instance_error_traceback("demo", "KeyError", "'socket'", database="lalouve_staging")

    assert captured == {"database": "lalouve_staging"}


def test_instance_log_analysis_window_covered_by_no_file(monkeypatch):
    _instance_with_logs(monkeypatch, [Path("/var/log/server.log")])
    monkeypatch.setattr(mcp_server.probes, "start_odoo_logs", lambda *_a, **_k: _FakeProc("[]"))

    assert mcp_server.instance_log_analysis("demo", "errors", since="2020-01-01") == "(no log file covers that window)"


def test_instance_log_analysis_window_relays_an_odoo_logs_without_list(monkeypatch):
    _instance_with_logs(monkeypatch, [Path("/var/log/server.log")])
    monkeypatch.setattr(
        mcp_server.probes,
        "start_odoo_logs",
        lambda *_a, **_k: _FakeProc("", "Error: No such command 'list'."),
    )

    assert mcp_server.instance_log_analysis("demo", "errors", since="2026-09-25") == "Error: No such command 'list'."


def test_instance_log_analysis_without_a_window_still_reads_every_file(monkeypatch):
    """Unchanged behaviour: no `list` pass, no window handed to odoo-logs."""
    everything = [Path("/var/log/server.log"), Path("/var/log/server.log.1")]
    _instance_with_logs(monkeypatch, everything)
    calls = []

    def fake_start(command, files, host, **kw):
        calls.append((command, files, kw))
        return _FakeProc("[]")

    monkeypatch.setattr(mcp_server.probes, "start_odoo_logs", fake_start)

    mcp_server.instance_log_analysis("demo", "errors")

    assert calls == [("errors", everything, {"since": None, "until": None, "database": None})]


def test_instance_log_files_no_such_instance(monkeypatch):
    monkeypatch.setattr(mcp_server, "_find", lambda *_: None)
    assert mcp_server.instance_log_files("demo") == "(no such instance)"


def test_instance_log_files_no_log_file(monkeypatch):
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: [])
    assert mcp_server.instance_log_files("demo") == "(no log file found)"


def test_instance_log_files_runs_odoo_logs_list_with_the_window(monkeypatch):
    """Same resolve/run/parse shape as instance_log_analysis; the window goes
    to odoo-logs itself, which keeps only the files that overlap it."""
    files = [Path("/var/log/server.log"), Path("/var/log/server.log.2026-09-25")]
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: files)

    captured = {}
    rows = [
        {"path": "/var/log/server.log.2026-09-25", "size": 1, "start": "2026-09-25 07:16:00", "end": "x", "note": ""}
    ]

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return json.dumps(rows), "Getting logs from 2026-09-25 19:50:00 to None"

    def fake_start(command, files, host, *, since=None, until=None, database=None):
        captured.update(command=command, files=files, since=since, until=until)
        return _FakeProc()

    monkeypatch.setattr(mcp_server.probes, "start_odoo_logs", fake_start)

    assert mcp_server.instance_log_files("demo", since="2026-09-25 19:50", until="2026-09-25 20:10") == rows
    assert captured == {"command": "list", "files": files, "since": "2026-09-25 19:50", "until": "2026-09-25 20:10"}


def test_instance_log_files_relays_an_odoo_logs_without_list(monkeypatch):
    """An older odoo-logs has no `list`: its own message beats a bare failure."""
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: [Path("/var/log/server.log")])

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", "Error: No such command 'list'."

    monkeypatch.setattr(mcp_server.probes, "start_odoo_logs", lambda *_a, **_k: _FakeProc())

    assert mcp_server.instance_log_files("demo") == "Error: No such command 'list'."


def test_instance_error_traceback_no_such_instance(monkeypatch):
    monkeypatch.setattr(mcp_server, "_find", lambda *_: None)
    assert mcp_server.instance_error_traceback("demo", "KeyError", "'socket'") == "(no such instance)"


def test_instance_error_traceback_no_log_file(monkeypatch):
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: [])
    assert mcp_server.instance_error_traceback("demo", "KeyError", "'socket'") == "(no log file found)"


def test_instance_error_traceback_no_match(monkeypatch):
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: [Path("/var/log/server.log")])
    monkeypatch.setattr(mcp_server.probes, "error_traceback", lambda *_a, **_k: "")
    assert mcp_server.instance_error_traceback("demo", "KeyError", "'socket'") == "(no matching traceback found)"


def test_instance_error_traceback_resolves_files_and_forwards_type_error(monkeypatch):
    """Same three-line shape as instance_log_analysis -- resolve, call,
    return -- just swapping in error_traceback."""
    monkeypatch.setattr(mcp_server, "_find", lambda *_: {"name": "demo"})
    monkeypatch.setattr(mcp_server.probes, "instance_log_files", lambda *_a, **_k: [Path("/var/log/server.log")])

    captured = {}

    def fake_error_traceback(files, error_type, error, host, *, since=None, until=None, database=None):
        captured["window"] = (since, until)
        captured["files"] = files
        captured["error_type"] = error_type
        captured["error"] = error
        return "Traceback (most recent call last):\nKeyError: 'socket'"

    monkeypatch.setattr(mcp_server.probes, "error_traceback", fake_error_traceback)

    result = mcp_server.instance_error_traceback("demo", "KeyError", "'socket'")

    assert result == "Traceback (most recent call last):\nKeyError: 'socket'"
    assert captured == {
        "window": (None, None),
        "files": [Path("/var/log/server.log")],
        "error_type": "KeyError",
        "error": "'socket'",
    }


def test_instance_error_traceback_with_a_window_reads_only_the_overlapping_files(monkeypatch):
    """Same file narrowing as instance_log_analysis, then the window itself
    goes on to the traceback scan."""
    _instance_with_logs(monkeypatch, [Path("/var/log/server.log"), Path("/var/log/server.log.2026-09-25")])
    monkeypatch.setattr(
        mcp_server.probes,
        "files_in_window",
        lambda files, since, until, host: [Path("/var/log/server.log.2026-09-25")],
    )
    captured = {}

    def fake_error_traceback(files, error_type, error, host, *, since=None, until=None, database=None):
        captured.update(files=files, since=since, until=until)
        return "KeyError: 'socket'"

    monkeypatch.setattr(mcp_server.probes, "error_traceback", fake_error_traceback)

    result = mcp_server.instance_error_traceback(
        "demo", "KeyError", "'socket'", since="2026-09-25 19:50", until="2026-09-25 20:10"
    )

    assert result == "KeyError: 'socket'"
    assert captured == {
        "files": [Path("/var/log/server.log.2026-09-25")],
        "since": "2026-09-25 19:50",
        "until": "2026-09-25 20:10",
    }


def test_instance_error_traceback_window_covered_by_no_file(monkeypatch):
    _instance_with_logs(monkeypatch, [Path("/var/log/server.log")])
    monkeypatch.setattr(mcp_server.probes, "files_in_window", lambda *_a, **_k: [])

    assert (
        mcp_server.instance_error_traceback("demo", "KeyError", "'socket'", since="2020-01-01")
        == "(no log file covers that window)"
    )


def test_mail_audit_has_no_include_sensitive_information_argument():
    assert "include_sensitive_information" not in inspect.signature(mcp_server.mail_audit).parameters


def test_mail_audit_honors_launch_time_flag_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **kw: captured.update(kw) or None)

    monkeypatch.setattr(mcp_server, "_include_sensitive_information", False)
    mcp_server.mail_audit("demo")
    assert captured["include_sensitive_information"] is False

    monkeypatch.setattr(mcp_server, "_include_sensitive_information", True)
    mcp_server.mail_audit("demo")
    assert captured["include_sensitive_information"] is True


def test_mail_audit_unwraps_the_single_nested_object(monkeypatch):
    # odoo-db's `mail` answers one nested object, not a flat row list -- kept
    # out of db_query's scoped command set for exactly this reason. The
    # subprocess layer still wraps it in a one-element list (every odoo-db
    # command does, see parse_odoo_db_output), so this must unwrap it rather
    # than handing the caller a single-item list like db_query would.
    class _FakeProc:
        def communicate(self, timeout=None):
            return '{"is_neutralized": false, "mail_servers": []}', ""

    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **_kw: _FakeProc())

    result = mcp_server.mail_audit("demo")
    assert result == {"is_neutralized": False, "mail_servers": []}


def test_mail_audit_returns_empty_dict_for_an_empty_row_list(monkeypatch):
    class _FakeProc:
        def communicate(self, timeout=None):
            return "[]", ""

    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **_kw: _FakeProc())

    assert mcp_server.mail_audit("demo") == {}


def test_mail_audit_returns_raw_text_for_non_json_output(monkeypatch):
    class _FakeProc:
        def communicate(self, timeout=None):
            return "", "database does not exist"

    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **_kw: _FakeProc())

    assert mcp_server.mail_audit("demo") == "database does not exist"


def test_mail_audit_reports_when_odoo_db_is_not_on_path(monkeypatch):
    monkeypatch.setattr(mcp_server.probes, "start_odoo_db", lambda *_a, **_kw: None)

    assert mcp_server.mail_audit("demo") == "(odoo-db not found on PATH)"


def test_odooly_tools_refuse_without_the_launch_time_flag(monkeypatch):
    """No tool call can turn odooly support on itself -- only the
    --enable-plugins=odooly CLI flag can, mirroring _include_sensitive_information."""
    monkeypatch.setattr(mcp_server, "_enabled_plugins", set())

    with pytest.raises(ValueError, match="--enable-plugins=odooly"):
        mcp_server.list_odooly_envs()
    with pytest.raises(ValueError, match="--enable-plugins=odooly"):
        mcp_server.instance_odooly_env("openerp-acme18-integration", "acme18_int")
    with pytest.raises(ValueError, match="--enable-plugins=odooly"):
        mcp_server.restore_app_icons("acme18-int")


def test_odooly_tools_delegate_to_the_plugin_once_enabled(monkeypatch):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"odooly"})
    monkeypatch.setattr(odooly_plugin, "read_odooly_envs", lambda: [{"name": "acme18-int", "db": "acme18_int"}])

    assert mcp_server.list_odooly_envs() == ["acme18-int"]
    assert mcp_server.instance_odooly_env("openerp-acme18-integration", "acme18_int") == "acme18-int"
    assert mcp_server.instance_odooly_env("openerp-acme18-integration", "nope") is None

    captured = {}
    monkeypatch.setattr(
        odooly_plugin,
        "run_odooly_script",
        lambda script, env, *extra: captured.update(script=script, env=env, extra=extra) or "ok",
    )
    assert mcp_server.create_test_job("acme18-int") == "ok"
    assert captured == {"script": "create_test_job", "env": "acme18-int", "extra": ()}

    assert mcp_server.send_test_mail("acme18-int", to="me@example.com") == "ok"
    assert captured == {"script": "send_test_mail", "env": "acme18-int", "extra": ("--to", "me@example.com")}


def test_send_test_mail_needs_a_recipient(monkeypatch):
    """The script's own `--to` is mandatory -- catch a missing recipient here
    rather than let it fail one subprocess hop away from a clear error."""
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"odooly"})

    with pytest.raises(ValueError, match="`to`"):
        mcp_server.send_test_mail("acme18-int", to="")


def test_pos_status_refuses_without_the_launch_time_flag(monkeypatch):
    """Same launch-time-only gate as odooly -- --enable-plugins=pos is what
    turns this on, never a tool call."""
    monkeypatch.setattr(mcp_server, "_enabled_plugins", set())

    with pytest.raises(ValueError, match="--enable-plugins=pos"):
        mcp_server.pos_status("acme18-int")


def test_pos_status_delegates_to_the_plugin_once_enabled(monkeypatch):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"pos"})
    monkeypatch.setattr(pos_plugin, "fetch_pos_status", lambda env: ([{"name": "Caisse 01"}], ""))

    assert mcp_server.pos_status("acme18-int") == [{"name": "Caisse 01"}]


def test_pos_status_raises_the_plugins_message_when_it_cannot_connect(monkeypatch):
    """`fetch_pos_status` reports failure as (None, message) for the TUI's
    tab body -- the MCP tool has no tab body, so it raises that same
    message instead of silently returning nothing."""
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"pos"})
    monkeypatch.setattr(pos_plugin, "fetch_pos_status", lambda env: (None, f"cannot connect to '{env}': boom"))

    with pytest.raises(ValueError, match="cannot connect to 'acme18-int': boom"):
        mcp_server.pos_status("acme18-int")


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
        assert isinstance(mcp_server.instance_log_analysis(name, "errors"), (list, str))
        assert isinstance(mcp_server.instance_log_files(name), (list, str))
        assert isinstance(mcp_server.instance_error_traceback(name, "KeyError", "'socket'"), str)

        dbs = mcp_server.instance_databases(name)
        if dbs and dbs.get("databases"):
            assert isinstance(mcp_server.mail_audit(dbs["databases"][0]), (dict, str))

    stats = mcp_server.host_stats()
    assert stats is None or isinstance(stats, dict)

    assert mcp_server.get_instance("__no_such_instance__") is None
    assert mcp_server.list_top("__no_such_instance__") == []
    # not exercised against a real instance: SIGQUIT is a live signal, not a
    # read — only the not-found path is safe to smoke-test unconditionally.
    assert mcp_server.instance_dump_stacks("__no_such_instance__") == {"error": "(no such instance)", "workers": []}


def test_instance_databases_reports_neutralization_per_database(monkeypatch):
    """The agent gets the same binary claim the TUI shows a human — and a db
    odoo-db couldn't answer for is simply absent from the map (unknown),
    never reported as a live database."""
    inst = {"name": "b.service", "status": "running", "uptime": "-", "manager": "systemd"}
    mcp_server._discovered.clear()  # the discovery cache outlives a single test
    monkeypatch.setattr(mcp_server.managers, "list_instances", lambda *_a, **_k: [inst])
    monkeypatch.setattr(mcp_server.probes, "databases_of", lambda *_a, **_k: (["staging", "prod"], "5432"))
    monkeypatch.setattr(mcp_server.probes, "pg_target_of", lambda *_a, **_k: mcp_server.probes.PgTarget())
    # `odoo-db list` answers for the whole cluster; only this instance's own
    # databases are reported back
    cluster = {"staging": True, "someone_elses": True}
    monkeypatch.setattr(mcp_server.probes, "neutralized_databases", lambda *_a, **_k: cluster)

    assert mcp_server.instance_databases("b.service") == {
        "databases": ["staging", "prod"],
        "db_port": "5432",
        "neutralized": {"staging": True},
    }
