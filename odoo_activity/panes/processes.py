"""Processes tab body — populates a Tree with a frozen snapshot of the
instance's top grouped by role. Unlike Top (re-rendered
every tick, sorted by live CPU%), this is only refreshed when the tab is
(re)opened -- see ActivityPane._render_active's Processes branch.
"""

from __future__ import annotations

from textual.widgets import Tree

from odoo_activity.probes import ProcRow

# Order is display order. cron (nice==10) and gevent ('gevent' in argv) are
# cross-checked against two independent monitoring tools and agree. master
# comes from instance_pid (the process manager's own answer), not a pid==1
# or ppid-iteration guess -- neither holds on a shared, multi-instance host.
# jobrunner is queue_job's worker, named by its own postgres connection (see
# jobrunner_pids) or by argv where setproctitle is installed.
# No dispatcher/shell/other bucket: rows only ever contains the master's own
# descendants (see instance_workers), so nothing outside these roles
# can appear -- the rest is exhaustively "http".
_ROLE_LABELS = {
    "master": "Main",
    "jobrunner": "Job Runner (queue_job)",
    "cron": "Cron Worker",
    "gevent": "Longpolling Worker (gevent)",
    "http": "HTTP Worker",
}

# odoo's own label for the worker, in argv only when setproctitle is
# installed -- when it is, no postgres lookup is needed to name it
_JOBRUNNER_TITLE = "WorkerJobRunner"


def _role(row: ProcRow, master: str | None, jobrunners: frozenset[str] = frozenset()) -> str:
    if row["pid"] == master:
        return "master"
    if _JOBRUNNER_TITLE in row["cmd"]:  # odoo's own label, when it has one
        return "jobrunner"
    # gevent and cron identify themselves out of argv/nice, which cannot be
    # wrong; the postgres lookup behind `jobrunners` is a heuristic, so it
    # only gets to name what those two didn't claim
    if "gevent" in row["cmd"]:
        return "gevent"
    if row["nice"] == "10":
        return "cron"
    if row["pid"] in jobrunners:
        return "jobrunner"
    return "http"


def render_processes(
    tree: Tree, rows: list[ProcRow], master: str | None, jobrunners: frozenset[str] = frozenset()
) -> None:
    """Populate `tree` with `rows` (instance_workers' odoo process list)
    grouped by role.

    Each leaf carries its own `ProcRow` as node data, which is what `K` picks
    the pid to kill out of (see ActivityPane.selected_process)."""
    tree.clear()
    grouped: dict[str, list[ProcRow]] = {role: [] for role in _ROLE_LABELS}
    for row in rows:
        grouped[_role(row, master, jobrunners)].append(row)

    for role, label in _ROLE_LABELS.items():
        members = grouped[role]
        if not members:
            continue

        group_node = tree.root.add(f"{label} ({len(members)})", expand=True)
        for row in members:
            group_node.add_leaf(f"pid {row['pid']} — {row['cmd']}", data=row)

    tree.root.expand()
