"""Stacks tab body — populates a Tree with the last (dump stacks) snapshot:
worker pid -> thread -> frames. The Tree itself is mounted directly as
ActivityPane's #acstacks (see panes/detail.py); no wrapper widget, so it's a
tab body like #acbody/#actable/#acraw, not a separate focus target.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from odoo_activity.probes import Thread, Worker, stacks_by_activity


def _short(path: str, workdir: Path) -> str:
    """`path` relative to the instance's workdir (py-spy shows paths
    relative to the addons sys.path entry; workdir is a one-config-read
    stand-in for that, not an exact match). Falls back to the bare
    filename for anything outside workdir (stdlib, venv)."""
    try:
        return str(Path(path).relative_to(workdir))
    except ValueError:
        return path.rsplit("/", 1)[-1]


def filter_workers(workers: list[Worker], needle: str) -> list[Worker]:
    """`workers` keeping only the threads matching `needle` (case-insensitive
    substring of the thread's name/db/url or of any frame's func/file), and
    only the workers left with a thread.

    A matching thread keeps all its frames — a stack cut down to the matching
    lines is no longer a stack.
    """
    needle = needle.lower()
    out: list[Worker] = []

    for worker in workers:
        threads = [t for t in worker["threads"] if _thread_matches(t, needle)]
        if threads:
            # cast, not Worker(pid=..., threads=...): a plain **-rebuild carries
            # along whatever fields Worker grows later instead of dropping them
            out.append(cast(Worker, {**worker, "threads": threads}))

    return out


def _thread_matches(thread: Thread, needle: str) -> bool:
    fields = [thread["name"], thread["db"], thread["url"]]
    fields += [part for frame in thread["frames"] for part in (frame["func"], frame["file"])]
    return any(needle in field.lower() for field in fields if field)


def render_stacks(tree: Tree, workers: list[Worker], workdir: Path) -> bool:
    """Populate `tree` with stack dump data from
    `probes.dump_and_parse_stacks` and sort by activity.

    Busy workers and threads are sorted first and expanded; idle ones stay
    collapsed.

    Returns True if any worker is busy, False if all are idle."""
    tree.clear()
    busy_any = False

    for worker in stacks_by_activity(workers):
        threads = worker["threads"]
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
    innermost = thread["frames"][0] if thread["frames"] else None
    where = f" — {innermost['func']} ({_short(innermost['file'], workdir)}:{innermost['line']})" if innermost else ""
    # db is only set while the thread is mid-request; qt/pt (see probes.py's
    # _THREAD_RE comment) show where a stuck request is actually spending
    # time — in SQL (qt) or outside it (pt).
    request = f" (db:{thread['db']} qt:{thread['query_time']} pt:{thread['python_time']})" if thread["db"] else ""
    node = parent.add(f"{tag} {thread['name']}{request}{where}", expand=not thread["idle"])
    # frames already innermost-first -- see probes.stacks_by_activity
    for frame in thread["frames"]:
        node.add_leaf(f"{_short(frame['file'], workdir)}:{frame['line']} in {frame['func']}")
