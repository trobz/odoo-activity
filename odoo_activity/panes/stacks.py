"""StacksPane — panel below the activity pane, showing the last (dump stacks)
snapshot as a tree: worker pid -> thread -> frames. Hidden (`display = False`,
same pattern as ActivityPane's #actable/#acraw) until a dump finds something
actually busy, so it costs nothing in the layout for sessions that never trigger
one — or where nothing was long-running.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
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


class StacksPane(Vertical):
    DEFAULT_CSS = """
    StacksPane { border: round $accent; background: transparent; height: 14; }
    StacksPane.-maximized { height: 1fr; }
    StacksPane:focus-within { border: round $primary; }
    StacksPane Tree { background: transparent; }
    """

    ALLOW_MAXIMIZE = True

    def compose(self) -> ComposeResult:
        yield Tree("stacks", id="stacks-tree")

    def on_mount(self) -> None:
        self.border_title = "Stacks"
        self.query_one(Tree).show_root = False
        self.display = False  # hidden until a dump finds something busy

    def show(self, workers: list[Worker], workdir: Path) -> bool:
        """Populate tree view with stack dump data from
        `probes.dump_and_parse_stacks` and sort by activity.

        Busy workers and threads are sorted first and expanded; idle ones stay
        collapsed. Shows the panel only if at least one thread is busy.

        Returns True if any worker is busy, False if all are idle."""
        tree = self.query_one(Tree)
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
                self._add_thread(pid_node, t, workdir)

        tree.root.expand()
        self.display = busy_any
        return busy_any

    def _add_thread(self, parent: TreeNode, thread: Thread, workdir: Path) -> None:
        tag = "[b red]busy[/]" if not thread["idle"] else "[dim]idle[/]"
        innermost = thread["frames"][-1] if thread["frames"] else None
        where = (
            f" — {innermost['func']} ({_short(innermost['file'], workdir)}:{innermost['line']})" if innermost else ""
        )
        node = parent.add(f"{tag} {thread['name']}{where}", expand=not thread["idle"])
        # frames come outermost-first (as kill -3 prints them); py-spy prints
        # innermost first so the concerning frame is the one you see without
        # scrolling — match that order here rather than the raw dump's.
        for frame in reversed(thread["frames"]):
            node.add_leaf(f"{_short(frame['file'], workdir)}:{frame['line']} in {frame['func']}")
