import asyncio
import json
import signal
from types import SimpleNamespace

from textual.widgets import DataTable

from odoo_activity import probes, tui
from odoo_activity.host import Host
from odoo_activity.panes import detail as detail_mod


def test_bar_colors_by_htop_thresholds():
    assert "[green]" in tui._bar(49)
    assert "[yellow]" in tui._bar(50)
    assert "[yellow]" in tui._bar(79)
    assert "[red]" in tui._bar(80)


def test_parse_odoo_db_output_falls_back_to_raw_for_non_json():
    rows, raw = probes.parse_odoo_db_output("queue_job module not installed.\n", "")
    assert rows is None
    assert raw == "queue_job module not installed."


def test_parse_odoo_db_output_wraps_a_single_json_object():
    rows, _raw = probes.parse_odoo_db_output('{"db": "demo"}', "")
    assert rows == [{"db": "demo"}]


def test_proc_cpu_ticks_many_batches_a_remote_host_into_one_call(monkeypatch):
    # regression: this used to be one ssh round trip per pid (proc_cpu_ticks
    # in a loop) -- slow enough with more than a couple of top that
    # the Top tab sat on "Loading top..." for real seconds.
    calls = []

    def fake_run(self, argv, **_):
        calls.append(argv)
        # pid "2" is gone -- cat's stderr is redirected, stdout for it is empty
        out = (
            "\x1e1\n1 (odoo) S 0 1 1 0 -1 0 0 0 0 0 111 222 0 0 20 0 1 0 1 0 0 0 0\n"
            "\x1e2\n"
            "\x1e3\n3 (postgres) S 0 1 1 0 -1 0 0 0 0 0 5 5 0 0 20 0 1 0 1 0 0 0 0\n"
        )
        return SimpleNamespace(stdout=out)

    monkeypatch.setattr(Host, "run", fake_run)
    host = Host(alias="x")

    result = probes.proc_cpu_ticks_many(["1", "2", "3"], host)

    assert result == {"1": 111 + 222, "2": None, "3": 5 + 5}
    assert len(calls) == 1  # one round trip, not three


def test_instance_action_routes_by_manager(monkeypatch):
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(probes.subprocess, "run", fake_run)
    assert probes.instance_action("demo.service", "restart") == ""
    probes.instance_action("openerp-odoo-staging", "stop", manager="supervisor")
    assert calls == [
        ["systemctl", "--user", "restart", "demo.service"],
        ["supervisorctl", "stop", "openerp-odoo-staging"],
    ]


def test_instance_action_odoosh_restarts_both_services(monkeypatch):
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(probes.subprocess, "run", fake_run)
    assert probes.instance_action("demo", "restart", manager="odoosh") == ""
    assert calls == [
        ["odoosh-restart", "http"],
        ["odoosh-restart", "cron"],
    ]
    assert probes.instance_action("demo", "start", manager="odoosh") != ""


def test_rebuild_instances_sorts_by_status_and_nests_dbs(monkeypatch):
    instances = [
        {"name": "c.service", "status": "stopped", "uptime": "-", "manager": "systemd"},
        {"name": "a.service", "status": "failed", "uptime": "-", "manager": "systemd"},
        {"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"},
    ]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(
        tui, "databases_of", lambda inst, *_: (["demo"], None) if inst["name"] == "b.service" else ([], None)
    )

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            keys = [item.name for item in pilot.app.query_one("#instances", tui.ListView).children]
            # running first, its db nested right under it, then failed, then stopped
            assert keys == [
                "systemd:b.service",
                "systemd:b.service::db::demo",
                "systemd:a.service",
                "systemd:c.service",
            ]

    asyncio.run(go())


def test_instance_action_waits_for_confirmation(monkeypatch):
    # regression guard: s/r and the buttons must not act until the user
    # confirms — this is the whole point of ConfirmScreen
    calls = []
    monkeypatch.setattr(
        tui,
        "list_instances",
        lambda *_: [{"name": "a.service", "status": "running", "uptime": "-", "manager": "systemd"}],
    )
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda _inst, *_: ([], None))
    monkeypatch.setattr(
        tui, "instance_action", lambda name, action, manager, *_: calls.append((name, action, manager)) or ""
    )

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 30)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("s")  # running -> toggles to stop, opens ConfirmScreen first
            await pilot.pause()
            assert calls == []
            assert isinstance(pilot.app.screen, tui.ConfirmScreen)

            await pilot.click("#confirm-yes")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == [("a.service", "stop", "systemd")]

    asyncio.run(go())


def test_instance_only_shortcuts_are_hidden_in_database_mode(monkeypatch):
    # regression guard: a db row resolves to its owning instance, so D/S have
    # an instance to act on there too -- mode is what must keep them hidden
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert pilot.app.check_action("dumpstacks", ()) is True
            assert pilot.app.check_action("copy_shell_command", ()) is True

            await pilot.press("down")  # onto the nested db row -> database mode
            await pilot.pause()
            assert pilot.app.check_action("dumpstacks", ()) is False
            assert pilot.app.check_action("copy_shell_command", ()) is False

            await pilot.press("up")
            await pilot.pause()
            assert pilot.app.check_action("dumpstacks", ()) is True

    asyncio.run(go())


def test_db_tab_search_and_show_all_gating(monkeypatch):
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: None)  # no real odoo-db call

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)

            # instance mode has no db table to widen
            assert pilot.app.check_action("toggle_show_all", ()) is False

            await pilot.press("down")  # database mode
            await pilot.pause()

            pane.select_tab_by_name("Modules")
            await pilot.pause()
            assert pilot.app.check_action("search", ()) is True
            assert pilot.app.check_action("toggle_show_all", ()) is True

            pane.select_tab_by_name("Locks")
            await pilot.pause()
            assert pilot.app.check_action("search", ()) is True  # every db table searches
            assert pilot.app.check_action("toggle_show_all", ()) is False

    asyncio.run(go())


def test_db_tab_retries_without_all_on_an_older_odoo_db(monkeypatch):
    # a host whose odoo-db predates `--all` on this command answers with a
    # usage error, not json -- the tab must still end up showing its rows
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))

    calls = []

    class _FakeProc:
        def __init__(self, out):
            self._out = out

        def communicate(self, timeout=None):
            return (self._out, "")

        def kill(self):
            pass

    def fake_start(command, db, port=None, host=None, *, include_sensitive_information=False, include_inactive=False):
        calls.append((command, include_inactive))
        if include_inactive:
            return _FakeProc("Error: No such option: --all\n")

        return _FakeProc('[{"name": "sale", "version": "16.0.1.0"}]')

    monkeypatch.setattr(detail_mod, "start_odoo_db", fake_start)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("down")  # database mode
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Modules")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert calls[-2:] == [("modules", True), ("modules", False)]
            assert pane._dbtab.rows == [{"name": "sale", "version": "16.0.1.0"}]
            # no `installed` key on those rows, so `A` just shows them all
            assert [i for i, _row in pane._visible_db_rows()] == [0]

            assert pane._dbtab.no_all is True
            notified = []
            monkeypatch.setattr(pilot.app, "notify", lambda msg, **_: notified.append(msg))
            pane.toggle_show_all()  # explains itself instead of silently doing nothing
            assert notified and "odoo-db" in notified[0]

    asyncio.run(go())


def test_db_tab_does_not_retry_on_an_unrelated_error(monkeypatch):
    """Only a "no such option" reply means this odoo-db predates `--all` --
    anything else (wrong db, no permission, ...) must surface as-is instead
    of being retried and doubling the wait before the user sees it."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))

    calls = []

    class _FakeProc:
        def __init__(self, out):
            self._out = out

        def communicate(self, timeout=None):
            return (self._out, "")

        def kill(self):
            pass

    def fake_start(command, db, port=None, host=None, *, include_sensitive_information=False, include_inactive=False):
        calls.append((command, include_inactive))
        return _FakeProc('database "demo" does not exist\n')

    monkeypatch.setattr(detail_mod, "start_odoo_db", fake_start)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("down")  # database mode
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Modules")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert calls == [("modules", True)]  # no second, plain-flag attempt
            assert pane._dbtab.rows == []
            assert pane._dbtab.no_all is False

    asyncio.run(go())


def test_visible_db_rows_filters_by_active_flag_and_query(monkeypatch):
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: None)  # no real odoo-db call

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("down")  # database mode
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Crons")
            await pilot.pause()

            pane._dbtab.rows = [
                {"name": "Mail: Fetchmail", "active": True},
                {"name": "Base: Auto-vacuum", "active": False},
                {"name": "Mail: Notify", "active": True},
            ]

            assert pane._visible_db_rows() == [(0, pane._dbtab.rows[0]), (2, pane._dbtab.rows[2])]

            pane.toggle_show_all()
            await pilot.pause()
            assert [i for i, _row in pane._visible_db_rows()] == [0, 1, 2]

            # indexes stay the ones into _dbtab.rows, so enter still opens the
            # right row's raw json
            pane._filters["Crons"] = "vacuum"
            assert [i for i, _row in pane._visible_db_rows()] == [1]

            pane._show_all = False
            assert pane._visible_db_rows() == []

    asyncio.run(go())


def test_selected_process_follows_the_filtered_table(monkeypatch):
    """`K`/`L` signal whatever selected_process() returns, so under a Top
    filter it must read the cursor's row *key*, not its position — those
    differ as soon as a row is filtered out."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: ([], None))

    def _row(pid: str, cmd: str) -> probes.ProcRow:
        return {"pid": pid, "ppid": "1", "user": "odoo", "mem": "0.1", "nice": "0", "cmd": cmd}

    # the Top tab's own periodic refresh (tick()) re-fetches on a real timer,
    # so it must return the same fixed rows every time -- anything live (the
    # real `ps`) would make the table's contents, and this test, a coin flip
    odoo_rows = [_row("101", "odoo-bin"), _row("103", "odoo-bin")]
    pg_rows = [_row("102", "postgres: demo")]
    monkeypatch.setattr(detail_mod, "instance_procs", lambda *_: (odoo_rows, pg_rows))
    monkeypatch.setattr(detail_mod, "proc_cpu_ticks_many", lambda *_: {})

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            assert pane.is_top_active()
            await pilot.app.workers.wait_for_complete()  # the Top tab's own initial fetch
            await pilot.pause()

            pane.open_search()
            await pilot.pause()
            await pilot.press(*"postgres")
            await pilot.press("enter")
            await pilot.pause()

            table = pilot.app.query_one("#actable", DataTable)
            assert table.row_count == 1
            table.move_cursor(row=0)
            selected = pane.selected_process()
            assert selected is not None
            assert selected["pid"] == "102"

    asyncio.run(go())


def test_leaving_a_late_db_tab_for_an_instance_row(monkeypatch):
    # regression guard: database mode has more tabs than instance mode, so
    # navigating off one of the extra ones (Crons) back up to an instance row
    # left _tab pointing past the end of the instance tabs
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("down")  # onto the nested db row -> database mode
            await pilot.pause()
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Crons")
            await pilot.pause()

            await pilot.press("up")  # back to the instance row
            await pilot.pause()

            assert pane._mode == "instance"
            assert pane._active_tab() in pane.TABS["instance"]

    asyncio.run(go())


_PARAMS_ROWS = [
    {"key": "base.url", "value": "http://example"},
    {"key": "database.secret", "value": "********"},
]


class _FakeOdooDbProc:
    """Stands in for the odoo-db subprocess.Popen `start_odoo_db` returns --
    `_fetch_db_tab` only ever calls .communicate()/.kill() on it."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return json.dumps(self._rows), ""

    def kill(self) -> None:
        pass


def _params_setup(monkeypatch):
    """One instance with one db, `odoo-db params` stubbed to _PARAMS_ROWS."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))
    monkeypatch.setattr(detail_mod, "db_port_of", lambda *_a, **_k: None)

    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: _FakeOdooDbProc(_PARAMS_ROWS))


async def _settle(pilot) -> None:
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def _open_params(pilot) -> "detail_mod.ActivityPane":
    await _settle(pilot)
    await pilot.press("down")  # onto the nested db row -> database mode
    await pilot.pause()
    pane = pilot.app.query_one(tui.ActivityPane)
    pane.select_tab_by_name("Params")
    await pilot.pause()
    await _settle(pilot)  # the fetch worker, then the call_after_refresh that populates the table
    return pane


def test_params_filter_keeps_original_row_index_as_key(monkeypatch):
    """Regression guard: _populate_datatable used to key rows by their
    position in the *filtered* list, so filtering down to one row and
    opening it could show the wrong row's raw json."""
    _params_setup(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            pane = await _open_params(pilot)

            pane.open_search()
            await pilot.pause()
            await pilot.press(*"secret")  # matches only _PARAMS_ROWS[1]
            await pilot.press("enter")
            await pilot.pause()

            table = pilot.app.query_one("#actable", DataTable)
            assert table.row_count == 1
            assert next(iter(table.rows)).value == "1"  # index into the unfiltered list, not 0

            shown = []
            orig_show_raw = pane._show_raw
            monkeypatch.setattr(pane, "_show_raw", lambda row: (shown.append(row), orig_show_raw(row)))

            await pilot.press("enter")  # open the surviving row's raw json
            await pilot.pause()

            assert shown == [_PARAMS_ROWS[1]]  # row 1, not row 0
            assert pane._showing_raw is True

    asyncio.run(go())


def test_on_resize_does_not_clobber_instance_log_with_leftover_params_filter(monkeypatch):
    """Regression guard for the on_resize hazard: _DbTab.abandon() (called by
    show_instance) drops .proc/.ident but not .rows, so a Params filter left
    over from an earlier database-mode visit stays around. Without the
    `self._mode == "database"` guard, on_resize would re-run
    _populate_datatable on that leftover state and, finding no match, call
    _log_body("(no match: ...)") -- clobbering the Logs/Config body on every
    terminal resize while sitting in instance mode."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane._mode = "instance"
            pane._tab = pane.TABS["instance"].index("Logs")

            # leftover from an earlier Params visit that no longer matches --
            # abandon() doesn't clear _dbtab.rows, and _filters["Params"] is
            # only popped by _load_db_tab, which instance mode never calls.
            pane._dbtab.rows = [{"key": "a", "value": "1"}]
            pane._filters["Params"] = "zzz"
            pane._showing_raw = False

            calls = []
            monkeypatch.setattr(pane, "_log_body", lambda text: calls.append(text))

            await pilot.resize_terminal(80, 30)
            await pilot.pause()

            assert calls == []  # on_resize must leave #acbody alone outside database mode

    asyncio.run(go())


def test_p_selects_params_in_database_mode_and_top_in_instance_mode(monkeypatch):
    """`p` is bound to both select_tab('Top') and select_tab('Params') --
    check_action's has_tab() gate (see tui.py) is what makes only one of them
    actually fire per mode, the same fallthrough Logs/Locks (`l`) and
    Config/Crons (`c`) already rely on."""
    _params_setup(monkeypatch)

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            app = pilot.app
            pane = app.query_one(tui.ActivityPane)

            assert app.check_action("select_tab", ("Top",)) is True
            assert app.check_action("select_tab", ("Params",)) is False

            await pilot.press("down")  # onto the nested db row -> database mode
            await pilot.pause()

            assert app.check_action("select_tab", ("Top",)) is False
            assert app.check_action("select_tab", ("Params",)) is True

            await pilot.press("p")
            await pilot.pause()
            assert pane._active_tab() == "Params"
            assert pane.has_search() is True

            pane.select_tab_by_name("Jobs")
            await pilot.pause()
            assert pane.has_search() is True


async def _settle(pilot):
    """Let the pane's workers finish.

    Not `workers.wait_for_complete()`: these flows deliberately supersede
    their own workers (a tab reload cancels the fetch in flight), and that
    call re-raises the cancellation as a failure.
    """
    for _ in range(20):
        await pilot.pause()


def _db_pilot(monkeypatch):
    """An app on a running instance with one db, ready to be walked into
    database mode with a single `down`."""
    instances = [{"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"}]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo"], None))
    monkeypatch.setattr(detail_mod, "db_port_of", lambda *_: None)


def test_jobs_tab_groups_by_function_then_drills_into_one_group(monkeypatch):
    """The point of the tab: a state count says the queue is backed up, the
    group rows say which function it's backed up on, and opening one says
    which jobs — with the dates that show how long they've been sitting."""
    groups = [{"function": "res.partner.export", "state": "started", "jobs": 2, "waiting": "02:00:00"}]
    members = [
        {"uuid": "aaa", "state": "started", "date_created": "2026-08-11T09:00:00", "date_started": None},
        {"uuid": "bbb", "state": "started", "date_created": "2026-08-11T09:05:00", "date_started": None},
    ]
    asked = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: (groups, ""))
    monkeypatch.setattr(
        detail_mod, "jobs_in_group", lambda db, function, state, *_: asked.append((function, state)) or (members, "")
    )

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")  # database mode
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)
            await pilot.pause()

            assert pane._dbtab.rows == groups
            assert pane._jobs_group is None

            # numbered so a long list can be counted by eye, and only data:
            # the tab's action lives in its own strip, not in the table
            table = pane.query_one("#actable", detail_mod.DataTable)
            assert [str(col.label) for col in table.columns.values()][:2] == ["#", "FUNCTION"]
            assert table.row_count == len(groups)
            assert str(table.get_row_at(0)[0]) == "1"

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.focus()  # escape below only reaches the pane from inside it
            table.action_select_cursor()  # enter on the group row
            await _settle(pilot)
            await pilot.pause()

            assert asked == [("res.partner.export", "started")]
            assert pane._jobs_group == ("res.partner.export", "started")
            assert pane._dbtab.rows == members

            await pilot.press("escape")  # back out to the groups
            await _settle(pilot)
            await pilot.pause()

            assert pane._jobs_group is None
            assert pane._dbtab.rows == groups

    asyncio.run(go())


def test_requeue_is_offered_on_an_empty_queue_and_waits_for_confirmation(monkeypatch):
    """The action is the reason to open the tab when everything is stuck, so
    it is offered with no data above it — and, like every other mutating
    action, does nothing until confirmed. It sits in its own strip rather
    than in the table: it acts on the database, not on a row."""
    calls = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: ([], ""))
    monkeypatch.setattr(detail_mod, "requeue_jobs", lambda db, *_: calls.append(db) or (7, ""))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)
            await pilot.pause()

            bar = pane.query_one("#acactions", detail_mod.Horizontal)
            buttons = list(bar.query(detail_mod.Button))
            assert bar.display is True and len(buttons) == 1

            # reloading the tab must not rebuild the buttons under the cursor
            pane._render_actions()
            assert list(bar.query(detail_mod.Button)) == buttons

            buttons[0].press()
            await pilot.pause()

            assert calls == []
            assert isinstance(pilot.app.screen, tui.ConfirmScreen)

            await pilot.click("#confirm-yes")
            await _settle(pilot)
            await pilot.pause()

            assert calls == ["demo"]

            pilot.app.query_one("#instances", tui.ListView).focus()  # `up` moves rows, not the cursor
            await pilot.press("up")  # back to the instance row
            await _settle(pilot)
            assert pane.query_one("#acactions", detail_mod.Horizontal).display is False

    asyncio.run(go())


def test_kill_works_from_the_processes_tab_on_the_pid_under_the_cursor(monkeypatch):
    """`K` was Top-only, so the job runner — the process you actually want to
    restart on purpose — could be seen but not killed."""
    workers = [
        {"pid": "10", "ppid": "1", "user": "odoo", "mem": "1.0", "nice": "0", "cmd": "odoo-bin"},
        {"pid": "11", "ppid": "10", "user": "odoo", "mem": "1.0", "nice": "0", "cmd": "odoo-bin"},
    ]
    killed = []
    snapshots = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(
        detail_mod, "instance_workers", lambda *_: snapshots.append(1) or ("10", workers[: 2 - len(killed)])
    )
    monkeypatch.setattr(detail_mod, "jobrunner_pids", lambda *_: {"11"})
    monkeypatch.setattr(tui, "signal_process", lambda pid, sig, *_: killed.append((pid, sig)))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Processes")
            await _settle(pilot)
            await pilot.pause()

            tree = pane.query_one("#acprocesses", detail_mod.Tree)
            labels = [str(node.label) for node in tree.root.children]
            assert labels == ["Main (1)", "Job Runner (queue_job) (1)"]

            # cursor onto the job runner's leaf, then kill it
            runner_leaf = tree.root.children[1].children[0]
            tree.select_node(runner_leaf)
            tree.cursor_line = runner_leaf.line
            selected = pane.selected_process()
            assert selected is not None and selected["pid"] == "11"

            taken_before = len(snapshots)
            await pilot.press("K")
            await pilot.pause()
            assert isinstance(pilot.app.screen, tui.ConfirmScreen)

            await pilot.click("#confirm-yes")
            await _settle(pilot)
            await pilot.pause()

            assert killed == [("11", signal.SIGKILL)]

            # the tree is a snapshot, so killing has to re-take it: otherwise
            # the dead pid sits on screen until the tab is reopened
            assert len(snapshots) > taken_before
            assert [str(node.label) for node in tree.root.children] == ["Main (1)"]

    asyncio.run(go())


def test_requeue_clears_the_dates_that_say_a_job_is_running(monkeypatch):
    """A row left with its `date_started` reads as running for as long as it
    then sits in the queue — the Jobs tab's `running` column is
    `age(now(), date_started)`. queue_job's own `set_pending` clears them,
    and so does this."""
    sent = []
    monkeypatch.setattr(
        Host,
        "run",
        lambda _self, _cmd, input_text=None: sent.append(input_text)
        or SimpleNamespace(returncode=0, stdout="UPDATE 3", stderr=""),
    )

    assert probes.requeue_jobs("demo") == (3, "")
    (sql,) = sent
    assert "state = 'pending'" in sql
    assert "date_started = NULL" in sql and "date_enqueued = NULL" in sql and "worker_pid = NULL" in sql
    # only the two states a job can be stuck in, from the one constant
    assert sql.endswith("WHERE state IN ('started', 'enqueued')")


def test_escape_after_leaving_database_mode_does_not_reopen_jobs(monkeypatch):
    """`escape` backs out of a Jobs group. Arrowing up to the instance row
    leaves that group behind, and escape there used to reload the Jobs tab
    over whatever instance tab was showing — querying the last database and
    rendering job rows under a heading like "Top"."""
    groups = [{"function": "res.partner.export", "state": "started", "jobs": 1}]
    fetched = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: fetched.append("groups") or (groups, ""))
    monkeypatch.setattr(detail_mod, "jobs_in_group", lambda *_: fetched.append("members") or ([], ""))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.focus()
            table.action_select_cursor()  # drill in
            await _settle(pilot)
            assert pane._jobs_group is not None

            pilot.app.query_one("#instances", tui.ListView).focus()  # `up` moves rows, not the cursor
            await pilot.press("up")  # instance mode
            await _settle(pilot)
            assert pane._mode == "instance"
            assert pane._jobs_group is None

            before = list(fetched)
            await pilot.press("escape")
            await _settle(pilot)

            assert pane._mode == "instance"
            assert fetched == before  # no database was queried

    asyncio.run(go())


def _ps_of(pids: dict[str, str], monkeypatch):
    """Stub the ps snapshot jobrunner_pids checks its candidates against."""
    by_pid = {
        pid: {"pid": pid, "ppid": "1", "user": "odoo", "mem": "0.1", "nice": "0", "cmd": cmd}
        for pid, cmd in pids.items()
    }
    monkeypatch.setattr(probes, "_ps_snapshot", lambda *_: (by_pid, {}))


def test_jobrunner_named_by_application_name_on_odoo_16_and_up(monkeypatch):
    """From 16.0 (odoo/odoo f6c13d7) odoo names its connections `odoo-<pid>`,
    which names the pid outright — no port to trace."""
    calls = []

    def fake_run(_self, argv, input_text=None):
        calls.append(argv)
        rows = '[{"app": "odoo-4242", "port": 50482}]'
        return SimpleNamespace(returncode=0, stdout=rows, stderr="")

    monkeypatch.setattr(Host, "run", fake_run)
    _ps_of({"4242": "/venv/bin/python /venv/bin/odoo -d demo"}, monkeypatch)

    assert probes.jobrunner_pids() == {"4242"}
    assert all(argv[0] == "psql" for argv in calls)  # no ss/lsof needed


def test_jobrunner_traced_by_client_port_when_odoo_never_named_it(monkeypatch):
    """13.0-15.0 never set application_name, so every backend reports ''. The
    connection is still identifiable by its statements; the pid behind it has
    to come from the TCP endpoint instead."""
    ss_out = (
        'ESTAB 0 0 127.0.0.1:50482 127.0.0.1:5432 users:(("python3",pid=4242,fd=12))\n'
        "ESTAB 0 0 127.0.0.1:5432 127.0.0.1:50482\n"  # postgres's own side, no holder
        'ESTAB 0 0 127.0.0.1:60000 10.0.0.9:443 users:(("firefox",pid=9999,fd=7))\n'
    )

    def fake_run(_self, argv, input_text=None):
        if argv[0] == "psql":
            # 60000 is another host's client port on this shared postgres,
            # and -1 is a unix socket: neither is ours to resolve
            rows = '[{"app": "", "port": 50482}, {"app": "", "port": 60000}, {"app": "", "port": -1}]'
            return SimpleNamespace(returncode=0, stdout=rows, stderr="")

        assert argv[:2] == ["ss", "-tnpH"]
        return SimpleNamespace(returncode=0, stdout=ss_out, stderr="")

    monkeypatch.setattr(Host, "run", fake_run)
    monkeypatch.setattr(probes, "odoo_pid_for_port", lambda *_: None)  # no lsof either
    _ps_of({"4242": "/venv/bin/python /venv/bin/odoo -d demo", "9999": "firefox"}, monkeypatch)

    # 60000 is live here, but on a browser socket to :443 -- not postgres
    assert probes.jobrunner_pids() == {"4242"}


def test_jobrunner_accepts_a_worker_odoo_renamed_with_setproctitle(monkeypatch):
    """setproctitle overwrites argv wholesale (`odoo: WorkerJobRunner 4242`),
    leaving neither entry point nor flag for `_looks_like_odoo` to match. The
    pid gate has to read that title as odoo — it is odoo naming itself — or
    it throws away the very process it was asked about."""

    def fake_run(_self, argv, input_text=None):
        assert argv[0] == "psql"
        return SimpleNamespace(returncode=0, stdout='[{"app": "odoo-4242", "port": 50482}]', stderr="")

    monkeypatch.setattr(Host, "run", fake_run)
    _ps_of({"4242": "odoo: WorkerJobRunner 4242"}, monkeypatch)

    assert probes.jobrunner_pids() == {"4242"}


def test_jobrunner_drops_a_pid_that_is_not_an_odoo_process(monkeypatch):
    """The bare `SELECT 1` in the query is the runner's keepalive, but also
    pgbouncer's and every monitor's ping. Whatever the connection turns out
    to belong to has to actually be odoo."""

    def fake_run(_self, argv, input_text=None):
        if argv[0] == "psql":
            return SimpleNamespace(returncode=0, stdout='[{"app": "", "port": 50482}]', stderr="")

        return SimpleNamespace(
            returncode=0,
            stdout='ESTAB 0 0 127.0.0.1:50482 127.0.0.1:5432 users:(("pgbouncer",pid=77,fd=9))\n',
            stderr="",
        )

    monkeypatch.setattr(Host, "run", fake_run)
    _ps_of({"77": "/usr/sbin/pgbouncer /etc/pgbouncer.ini"}, monkeypatch)

    assert probes.jobrunner_pids() == set()


def test_client_ports_fall_back_to_lsof_and_tolerate_neither(monkeypatch):
    """`ss` isn't everywhere, and neither is `lsof`. A port nobody can
    account for is left out rather than guessed at."""
    monkeypatch.setattr(Host, "run", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError))
    monkeypatch.setattr(probes, "odoo_pid_for_port", lambda port, _host=None: "7" if port == "50482" else None)

    assert probes._pids_by_client_port(["50482", "60000"]) == {"50482": "7"}

    monkeypatch.setattr(probes, "odoo_pid_for_port", lambda *_: None)
    assert probes._pids_by_client_port(["50482"]) == {}


def test_a_jobs_reload_keeps_its_action_strip_and_empties_its_rows(monkeypatch):
    """Two things a reload used to get wrong: it tore the strip down and
    rebuilt it (the blink `_render_actions` exists to avoid, and a race with
    the un-awaited removal), and an empty result left the previous depth's
    rows in the cache, which a resize or a search would then repaint inside
    the drilled-in view."""
    groups = [{"function": "res.partner.export", "state": "started", "jobs": 2}]
    _db_pilot(monkeypatch)
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: (groups, ""))
    monkeypatch.setattr(detail_mod, "jobs_in_group", lambda *_: ([], ""))  # the group emptied meanwhile

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)

            bar = pane.query_one("#acactions", detail_mod.Horizontal)
            button = next(iter(bar.query(detail_mod.Button)))

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.focus()
            table.action_select_cursor()  # drill in
            await _settle(pilot)

            assert list(bar.query(detail_mod.Button)) == [button]  # same widget, not a rebuild
            assert pane._dbtab.rows == []  # not the group rows the tab was showing

    asyncio.run(go())


def test_refresh_stays_in_the_group_it_was_refreshing(monkeypatch):
    """`R` re-fetches what is on screen. Drilled into a group, that is the
    group — backing out to the list is what `escape` is for."""
    fetched = []
    _db_pilot(monkeypatch)
    monkeypatch.setattr(
        detail_mod, "job_groups", lambda *_: fetched.append("groups") or ([{"function": "a.b", "state": "done"}], "")
    )
    monkeypatch.setattr(detail_mod, "jobs_in_group", lambda *_: fetched.append("members") or ([{"uuid": "x"}], ""))

    async def go():
        async with tui.OdooActivity().run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Jobs")
            await _settle(pilot)

            table = pane.query_one("#actable", detail_mod.DataTable)
            table.focus()
            table.action_select_cursor()
            await _settle(pilot)
            assert pane._jobs_group is not None

            fetched.clear()
            pane.refresh_active()
            await _settle(pilot)

            assert fetched == ["members"]  # not "groups"
            assert pane._jobs_group is not None

    asyncio.run(go())
