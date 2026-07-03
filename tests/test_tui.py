import asyncio
from types import SimpleNamespace

from odoo_activity import probes, tui


def test_systemd_instances_filters_templates_and_maps_status(monkeypatch):
    files = (
        "gnome@.service           disabled enabled\n"  # template, dropped
        "wiki.service             enabled  enabled\n"  # not odoo, filtered
        "openerp-demo.service    disabled enabled\n"  # matched by name only
        "odoo-demo.service        disabled enabled\n"
        "odoo-crashed.service     disabled enabled\n"
    )
    show = (
        "Id=wiki.service\nDescription=A wiki\nActiveState=inactive\n\n"
        "Id=openerp-demo.service\nDescription=Staging\nActiveState=inactive\n\n"
        "Id=odoo-demo.service\nDescription=Odoo odoo 18.0 instance\n"
        "ActiveState=active\nActiveEnterTimestampMonotonic=1000000\n\n"
        "Id=odoo-crashed.service\nDescription=Odoo crashed instance\nActiveState=failed\n"
    )

    def fake_run(cmd, **_):
        return SimpleNamespace(stdout=files if "list-unit-files" in cmd else show)

    monkeypatch.setattr(probes.subprocess, "run", fake_run)
    monkeypatch.setattr(probes.time, "clock_gettime", lambda _clk: 61.0)  # 60s after entering active

    assert probes.systemd_instances() == [
        {"name": "openerp-demo.service", "status": "stopped", "uptime": "-", "manager": "systemd"},
        {"name": "odoo-demo.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"},
        {"name": "odoo-crashed.service", "status": "failed", "uptime": "-", "manager": "systemd"},
    ]


def test_supervisor_instances_maps_status_vocab_and_uptime(monkeypatch, tmp_path):
    monkeypatch.setattr(probes, "SUPERVISOR_CONFD", tmp_path / "absent")  # status only
    status = (
        "/usr/bin/supervisorctl:6: DeprecationWarning: pkg_resources is deprecated\n"
        "  from pkg_resources import load_entry_point\n"
        "mailhog                        RUNNING   pid 23107, uptime 9:19:07\n"  # not odoo, filtered
        "openerp-odoo-staging           RUNNING   pid 19841, uptime 0:05:00\n"
        "openerp-odoo-crashed           FATAL     Exited too quickly\n"
        "openerp-odoo-exited            EXITED    Jul 02 10:59 AM\n"
        "openerp-odoo18-staging         STOPPED   Not started\n"
    )
    monkeypatch.setattr(probes.subprocess, "run", lambda *_, **__: SimpleNamespace(stdout=status))
    assert probes.supervisor_instances() == [
        {
            "name": "openerp-odoo-crashed",
            "status": "fatal",
            "uptime": "-",
            "manager": "supervisor",
            "command": "",
            "directory": "",
        },
        {
            "name": "openerp-odoo-exited",
            "status": "exited",
            "uptime": "-",
            "manager": "supervisor",
            "command": "",
            "directory": "",
        },
        {
            "name": "openerp-odoo-staging",
            "status": "running",
            "uptime": "0:05:00",
            "manager": "supervisor",
            "command": "",
            "directory": "",
        },
        {
            "name": "openerp-odoo18-staging",
            "status": "stopped",
            "uptime": "-",
            "manager": "supervisor",
            "command": "",
            "directory": "",
        },
    ]


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


def test_compute_status_promotes_ambiguous_stopped_but_not_explicit_failure(monkeypatch):
    # a live process promotes an ambiguous "stopped" report to running
    monkeypatch.setattr(tui, "procs_of", lambda _: [{"pid": "1"}])
    assert tui._compute_status({"status": "stopped"}) == "running"

    # regression: an explicit failure is authoritative even with a live
    # process matching the same db — procs_of() matches by db name, not
    # manager, so that process may belong to the *other* manager's instance
    assert tui._compute_status({"status": "failed"}) == "failed"

    monkeypatch.setattr(tui, "procs_of", lambda _: [])
    assert tui._compute_status({"status": "stopped"}) == "stopped"


def test_rebuild_instances_sorts_by_status_and_nests_dbs(monkeypatch):
    instances = [
        {"name": "c.service", "status": "stopped", "uptime": "-", "manager": "systemd"},
        {"name": "a.service", "status": "failed", "uptime": "-", "manager": "systemd"},
        {"name": "b.service", "status": "running", "uptime": "0:01:00", "manager": "systemd"},
    ]
    monkeypatch.setattr(tui, "list_instances", lambda: instances)
    monkeypatch.setattr(tui, "procs_of", lambda _: [])
    monkeypatch.setattr(
        tui, "databases_of", lambda inst: (["demo"], None) if inst["name"] == "b.service" else ([], None)
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
        lambda: [{"name": "a.service", "status": "running", "uptime": "-", "manager": "systemd"}],
    )
    monkeypatch.setattr(tui, "procs_of", lambda _: [])
    monkeypatch.setattr(tui, "databases_of", lambda _inst: ([], None))
    monkeypatch.setattr(
        tui, "instance_action", lambda name, action, manager: calls.append((name, action, manager)) or ""
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
