import asyncio
from types import SimpleNamespace

from odoo_activity import probes, tui
from odoo_activity.host import Host


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
