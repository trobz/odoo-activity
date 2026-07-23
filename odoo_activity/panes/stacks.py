"""Stacks tab body — populates a Tree with the last (dump stacks) snapshot:
worker pid -> thread -> frames. The Tree itself is mounted directly as
ActivityPane's #acstacks (see panes/detail.py); no wrapper widget, so it's a
tab body like #acbody/#actable/#acraw, not a separate focus target.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from odoo_activity.probes import Thread, Worker


def _short(path: str, workdir: Path) -> str:
    """`path` relative to the instance's workdir (py-spy shows paths
    relative to the addons sys.path entry; workdir is a one-config-read
    stand-in for that, not an exact match). Falls back to the bare
    filename for anything outside workdir (stdlib, venv)."""
    try:
        return str(Path(path).relative_to(workdir))
    except ValueError:
        return path.rsplit("/", 1)[-1]


def render_stacks(tree: Tree, workers: list[Worker], workdir: Path) -> bool:
    """Populate `tree` with stack dump data from
    `probes.dump_and_parse_stacks` and sort by activity.

    Busy workers and threads are sorted first and expanded; idle ones stay
    collapsed.

    Returns True if any worker is busy, False if all are idle."""
    tree.clear()
    busy_any = False

    by_busy_count = sorted(workers, key=lambda w: sum(not t["idle"] for t in w["threads"]), reverse=True)

    for worker in by_busy_count:
        threads = sorted(worker["threads"], key=lambda t: t["idle"])
        busy = sum(not t["idle"] for t in threads)
        busy_any = busy_any or busy > 0
        pid_node = tree.root.add(
            f"pid {worker['pid']} — {busy} busy / {len(threads) - busy} idle",
            expand=bool(busy),
        )
        for t in threads:
            _add_thread(pid_node, t, workdir)

    tree.root.expand()
    return busy_any


def _add_thread(parent: TreeNode, thread: Thread, workdir: Path) -> None:
    tag = "[b red]busy[/]" if not thread["idle"] else "[dim]idle[/]"
    innermost = thread["frames"][-1] if thread["frames"] else None
    where = f" — {innermost['func']} ({_short(innermost['file'], workdir)}:{innermost['line']})" if innermost else ""
    node = parent.add(f"{tag} {thread['name']}{where}", expand=not thread["idle"])
    # frames come outermost-first (as kill -3 prints them); py-spy prints
    # innermost first so the concerning frame is the one you see without
    # scrolling — match that order here rather than the raw dump's.
    for frame in reversed(thread["frames"]):
        node.add_leaf(f"{_short(frame['file'], workdir)}:{frame['line']} in {frame['func']}")
