"""Summary tab body — populates a Tree with a frozen snapshot of the
instance's top grouped by role. Unlike Top (re-rendered
every tick, sorted by live CPU%), this is only refreshed when the tab is
(re)opened -- see ActivityPane._render_active's Summary branch.
"""

from __future__ import annotations

from textual.widgets import Tree

from odoo_activity.probes import ProcRow

# Order is display order. cron (nice==10) and gevent ('gevent' in argv) are
# cross-checked against two independent monitoring tools and agree. master
# comes from instance_pid (the process manager's own answer), not a pid==1
# or ppid-iteration guess -- neither holds on a shared, multi-instance host.
# No dispatcher/shell/other bucket: rows only ever contains the master's own
# descendants (see instance_workers), so nothing outside these four roles
# can appear -- the rest is exhaustively "http".
_ROLE_LABELS = {
    "master": "Main",
    "cron": "Cron Worker",
    "gevent": "Longpolling Worker (gevent)",
    "http": "HTTP Worker",
}


def _role(row: ProcRow, master: str | None) -> str:
    if row["pid"] == master:
        return "master"
    if "gevent" in row["cmd"]:
        return "gevent"
    if row["nice"] == "10":
        return "cron"
    return "http"


def render_summary(tree: Tree, rows: list[ProcRow], master: str | None) -> None:
    """Populate `tree` with `rows` (instance_workers' odoo process list)
    grouped by role."""
    tree.clear()
    grouped: dict[str, list[ProcRow]] = {role: [] for role in _ROLE_LABELS}
    for row in rows:
        grouped[_role(row, master)].append(row)

    for role, label in _ROLE_LABELS.items():
        members = grouped[role]
        if not members:
            continue

        group_node = tree.root.add(f"{label} ({len(members)})", expand=True)
        for row in members:
            group_node.add_leaf(f"pid {row['pid']} — {row['cmd']}")

    tree.root.expand()
