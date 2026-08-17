from odoo_activity.panes.processes import _role
from odoo_activity.probes import ProcRow


def _row(pid: str, nice: str, cmd: str) -> ProcRow:
    return {"pid": pid, "ppid": "1", "user": "odoo", "mem": "0.1", "nice": nice, "cmd": cmd}


def test_role_classification():
    """Cross-checked against emoi's monitor.py and camptocamp's
    psutils_helpers.py: nice==10 is cron, 'gevent' in argv is longpolling,
    the master pid (from instance_pid, not guessed) wins over both, and
    everything else is an http worker."""
    master = "1"

    assert _role(_row("1", "0", "odoo-bin"), master) == "master"
    assert _role(_row("2", "10", "odoo-bin"), master) == "cron"
    assert _role(_row("3", "0", "odoo-bin --gevent-port=8072"), master) == "gevent"
    assert _role(_row("4", "0", "odoo-bin"), master) == "http"

    # nice==10 wins over gevent-substring iff both matched? cron is checked
    # after gevent, so a (hypothetical) 'gevent' hit is decided first --
    # pin that order since it's the one place the two branches could race
    assert _role(_row("5", "10", "odoo-bin --gevent-port=8072"), master) == "gevent"


def test_a_postgres_guess_never_outranks_what_argv_and_nice_state(monkeypatch):
    """`jobrunners` comes from matching statements on pg_stat_activity, which
    can name a worker that merely touched the queue_job table. gevent and
    cron identify themselves out of argv/nice and cannot be wrong, so the
    guess only gets to label what they didn't claim."""
    master = "1"
    runners = frozenset({"2", "3", "4"})

    assert _role(_row("2", "10", "odoo-bin"), master, runners) == "cron"
    assert _role(_row("3", "0", "odoo-bin --gevent-port=8072"), master, runners) == "gevent"
    assert _role(_row("4", "0", "odoo-bin"), master, runners) == "jobrunner"

    # odoo's own label (setproctitle installed) is not a guess -- it wins
    assert _role(_row("5", "10", "odoo: WorkerJobRunner 5"), master) == "jobrunner"
