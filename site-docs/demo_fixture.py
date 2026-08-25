"""Monkeypatches odoo_activity's probes with fixture data, then runs the
real interactive TUI -- for demo.tape, which needs the homepage to show a
populated instance/database. A bare `oa` on the recording box finds nothing
real to point at (no systemd/supervisor Odoo instance there), so it would
just show an empty Instances panel -- not a useful demo.

Not shipped in the package; only used by `make demo` at record time.
"""

from odoo_activity import probes, tui
from odoo_activity.panes import detail as detail_mod

INSTANCE = {
    "name": "openerp-acme18-production.service",
    "status": "running",
    "uptime": "4:12:07",
    "manager": "systemd",
}
DB = "acme18_prod"
MASTER_PID = "12340"

WORKER_ROWS = [
    {
        "pid": MASTER_PID,
        "ppid": "1",
        "user": "openerp",
        "mem": "0.5",
        "nice": "0",
        "cmd": "/usr/bin/python3 /opt/odoo/odoo-bin -c odoo.conf",
    },
    {"pid": "12341", "ppid": MASTER_PID, "user": "openerp", "mem": "3.1", "nice": "0", "cmd": "odoo: worker 0"},
    {"pid": "12342", "ppid": MASTER_PID, "user": "openerp", "mem": "2.9", "nice": "0", "cmd": "odoo: worker 1"},
    {"pid": "12343", "ppid": MASTER_PID, "user": "openerp", "mem": "1.8", "nice": "10", "cmd": "odoo: worker 2"},
    {
        "pid": "12344",
        "ppid": MASTER_PID,
        "user": "openerp",
        "mem": "2.2",
        "nice": "0",
        "cmd": "odoo: gevent worker 0",
    },
    {"pid": "12345", "ppid": MASTER_PID, "user": "openerp", "mem": "1.5", "nice": "0", "cmd": "odoo: WorkerJobRunner"},
]
PG_ROWS = [
    {
        "pid": "9001",
        "ppid": "1",
        "user": "postgres",
        "mem": "1.1",
        "nice": "0",
        "cmd": "postgres: acme18_prod openerp [local] idle",
    },
    {
        "pid": "9002",
        "ppid": "1",
        "user": "postgres",
        "mem": "0.9",
        "nice": "0",
        "cmd": "postgres: acme18_prod openerp [local] SELECT",
    },
]
JOB_GROUPS = [
    {"function": "res.partner.export", "state": "started", "jobs": 2, "waiting": "02:00:00"},
    {"function": "sale.order.send_invoice", "state": "pending", "jobs": 14, "waiting": "00:12:00"},
    {"function": "stock.picking.validate", "state": "failed", "jobs": 1, "waiting": "05:40:00"},
]
LONG_QUERIES_ROWS = [
    {
        "pid": 21044,
        "duration": "00:02:14",
        "state": "active",
        "query": "SELECT * FROM sale_order_line WHERE order_id = ANY(%s)",
    },
]


class _FakeOdooDbProc:
    """Stands in for the odoo-db subprocess `start_odoo_db` returns --
    switching to the nested db row auto-selects the Queries tab, and other
    db-mode tabs are one keypress away, so every odoo-db-backed command
    needs a safe fallback or the real subprocess call crashes the recording
    (no real odoo-db/psql on this box -- see the git history for the
    FileNotFoundError this fixture used to leave uncaught)."""

    def __init__(self, payload):
        self._payload = payload

    def communicate(self, timeout=None):
        import json

        return json.dumps(self._payload), ""

    def kill(self):
        pass


def main() -> None:
    tui.list_instances = lambda *_: [INSTANCE]
    probes.procs_of = lambda *_: []
    tui.databases_of = lambda *_: ([DB], None)

    detail_mod.instance_procs = lambda *_: (WORKER_ROWS[1:], PG_ROWS)
    detail_mod.proc_cpu_ticks_many = lambda pids, *_: dict.fromkeys(pids, 12345)
    detail_mod.instance_workers = lambda *_: (MASTER_PID, WORKER_ROWS)
    detail_mod.db_port_of = lambda *_: "5432"
    detail_mod.jobrunner_pids = lambda *_: set()
    detail_mod.job_groups = lambda *_a, **_k: (JOB_GROUPS, "")
    detail_mod.long_queries = lambda *_a, **_k: LONG_QUERIES_ROWS
    detail_mod.start_odoo_db = lambda *_a, **_k: _FakeOdooDbProc([])

    tui.OdooActivity(include_sensitive_information=True).run()


if __name__ == "__main__":
    main()
