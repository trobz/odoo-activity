"""odoo-activity TUI — host stats, instances (with their dbs), activity pane.

The app here is just the shell: it lays out the rows and wires focus, selection
and the refresh timers. The system data lives in :mod:`odoo_activity.probes`;
the mode-switched instance-mode and db-mode tabs are in
:mod:`odoo_activity.panes.detail`.
"""

from __future__ import annotations

import asyncio
import signal
from typing import ClassVar

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Footer, Label, ListItem, ListView, Static

from odoo_activity.panes.detail import ActivityPane
from odoo_activity.probes import (
    databases_of,
    dump_all_stacks,
    format_duration,
    instance_action,
    list_instances,
    procs_of,
    read_cpu_times,
    read_loadavg,
    read_mem,
    read_uptime,
    signal_process,
)

# sort priority for the instances list: running first, then a failure state
# (systemd "failed", supervisor "exited"/"fatal"), then a clean "stopped"
_STATUS_ORDER = {"running": 0, "stopped": 2}

# Trobz brand palette (see trobz brand-guidelines skill)
TROBZ_THEME = Theme(
    name="trobz",
    primary="#E54F0D",
    accent="#FFFFFF",  # distinct from primary so :focus borders are visible
    background="#1A110E",
    surface="#311E18",
    panel="#311E18",
    foreground="#FFFFFF",
    dark=True,
)


def _compute_status(inst: dict) -> str:
    # a manager may report "stopped" while a bare shell runs it, so a live
    # process promotes an ambiguous *stopped* report to running. An explicit
    # failure (systemd "failed", supervisor "exited"/"fatal") is authoritative
    # even if a process serving the same db is alive — procs_of() matches by
    # db name, not manager, so that process may belong to the *other*
    # manager's instance of the same name/db (see list_instances).
    if inst["status"] == "running":
        return "running"
    if inst["status"] == "stopped" and procs_of(inst):
        return "running"
    return inst["status"]


def _display_name(inst: dict) -> str:
    """Instance name for display — `.service` is systemd-unit plumbing, not
    part of the name a user recognizes."""
    return inst["name"].removesuffix(".service")


def _db_label(db: str, port: str | None, name_width: int, uptime_width: int, indent: int) -> str:
    """`dbname            port` — port's right edge lands on the same column
    as the instance rows' uptime right edge (dot + space + name_width + space
    + the uptime field) regardless of `indent`, not a fixed column."""
    if not port:
        return db

    pad = max(1, name_width + uptime_width + 1 - indent - len(db) - len(port))
    return f"{db}{' ' * pad}[dim]{port}[/]"


def _bar(pct: float, width: int = 24) -> str:
    """htop-style bar: green/yellow/red fill by load, dim track."""
    filled = min(width, round(pct / 100 * width))
    color = "red" if pct >= 80 else "yellow" if pct >= 50 else "green"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No popup. Dismisses with the chosen bool."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 50; height: auto;
        border: round $accent; background: $surface;
        padding: 1;
    }
    #confirm-msg { margin-bottom: 1; text-align: center; }
    #confirm-buttons { height: 3; align: center middle; }
    #confirm-buttons Button { margin: 0 1; }
    """

    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", id="confirm-yes", variant="error")
                yield Button("No", id="confirm-no", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class OdooActivity(App):
    CSS = """
    #body { height: 1fr; }

    #stats-row { height: 4; }
    .stat-panel { border: round $accent; width: 1fr; padding: 0 1; }
    .stat-title { width: 1fr; }
    .stat-value { width: auto; text-style: bold; }
    #uptime-text { height: 2; }

    #instances { border: round $accent; background: transparent; height: 6; }
    #activity { height: 1fr; }
    #instances:focus { border: round $primary; }
    /* fixed height 6 is right for the normal layout, but maximize should
       fill the screen like every other pane, not center at that height */
    #instances.-maximized { height: 1fr; }

    /* selected item stays visible whether or not its list has focus;
       color: auto keeps the text readable on top of the accent background */
    ListView { background: transparent; }
    ListView > ListItem.-highlight { background: $panel; color: auto; }
    ListView:focus > ListItem.-highlight { background: $accent; color: auto; }

    /* mouse text-selection: Textual defaults its foreground to transparent,
       which hides the selected text — force a readable one */
    .screen--selection { background: $primary; color: $text; }
    """

    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("s", "toggle_start_stop", "Start/Stop"),
        ("r", "restart", "Restart"),
        ("[", "prev_tab", "Prev tab"),
        ("]", "next_tab", "Next tab"),
        ("p", "select_tab('Processes')", "Processes"),
        ("l", "select_tab('Logs')", "Logs"),
        ("l", "select_tab('Locks')", "Locks"),
        ("c", "select_tab('Config')", "Config"),
        ("c", "select_tab('Crons')", "Crons"),
        ("u", "select_tab('Users')", "Users"),
        ("j", "select_tab('Jobs')", "Jobs"),
        ("slash", "search", "Search"),
        ("K", "kill_process", "Kill -9"),
        ("L", "quit_process", "Log dump -3"),
        ("D", "dumpstacks", "Dump stacks"),
        ("e", "toggle_config_mode", "Compact/Explain/Expand/Clean"),
        ("f", "toggle_maximize", "Maximize"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="body"):
            with Horizontal(id="stats-row"):
                with Vertical(id="cpu-panel", classes="stat-panel"):
                    with Horizontal():
                        yield Static("CPU", classes="stat-title")
                        yield Static("", id="cpu-pct", classes="stat-value")
                    yield Static("", id="cpu-bar")

                with Vertical(id="mem-panel", classes="stat-panel"):
                    with Horizontal():
                        yield Static("MEM", classes="stat-title")
                        yield Static("", id="mem-pct", classes="stat-value")
                    yield Static("", id="mem-bar")

                with Vertical(id="swap-panel", classes="stat-panel"):
                    with Horizontal():
                        yield Static("SWAP", classes="stat-title")
                        yield Static("", id="swap-pct", classes="stat-value")
                    yield Static("", id="swap-bar")

                with Vertical(id="uptime-panel", classes="stat-panel"):
                    yield Static("", id="uptime-text")

            yield ListView(id="instances")
            yield ActivityPane(id="activity")

        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(TROBZ_THEME)
        self.theme = "trobz"

        self.query_one("#instances", ListView).border_title = "Instances"

        self._cpu = read_cpu_times()
        self._instances: dict[str, dict] = {}
        self._instance_status: dict[str, str] = {}
        self._row_owner: dict[str, str] = {}  # row key -> owning instance key
        self._row_db: dict[str, str] = {}  # db row key -> db name
        self._shown_key: str | None = None  # highlighted row driving the activity pane
        self._pulse_on = True

        self.refresh_instances()

        self.query_one("#instances", ListView).focus()

        self.set_interval(1.0, self.refresh_host)
        self.set_interval(0.5, self.query_one(ActivityPane).poll)
        self.set_interval(5.0, self.poll_instances)
        self.set_interval(0.7, self._pulse_running)

    def on_show(self) -> None:
        """Run after layout is complete and app is shown."""
        self.refresh_host()

    def _get_bar_width(self, panel_id: str) -> int:
        """Get the available width for a bar in a stat panel."""
        try:
            panel = self.query_one(f"#{panel_id}", Vertical)
            return max(10, panel.size.width)
        except Exception:
            return 24

    def refresh_host(self) -> None:
        total, idle = read_cpu_times()
        d_total = total - self._cpu[0]
        d_idle = idle - self._cpu[1]
        self._cpu = (total, idle)

        cpu_pct = (1 - d_idle / d_total) * 100 if d_total else 0.0
        mem_pct, swap_pct = read_mem()

        self.query_one("#cpu-pct", Static).update(f"{cpu_pct:4.1f}%")
        self.query_one("#cpu-bar", Static).update(_bar(cpu_pct, self._get_bar_width("cpu-panel")))
        self.query_one("#mem-pct", Static).update(f"{mem_pct:4.1f}%")
        self.query_one("#mem-bar", Static).update(_bar(mem_pct, self._get_bar_width("mem-panel")))
        self.query_one("#swap-pct", Static).update(f"{swap_pct:4.1f}%")
        self.query_one("#swap-bar", Static).update(_bar(swap_pct, self._get_bar_width("swap-panel")))

        load1, load5, load15 = read_loadavg()
        self.query_one("#uptime-text", Static).update(
            f"uptime     {format_duration(read_uptime())}\nload avg   {load1:.2f} {load5:.2f} {load15:.2f}"
        )

        self.query_one(ActivityPane).tick()

    def refresh_instances(self) -> None:
        """Rebuild the instances+dbs list (initial load / membership change)."""
        self._rebuild_instances()

    @work(exclusive=True, group="instances")
    async def _rebuild_instances(self) -> None:
        lv = self.query_one("#instances", ListView)
        keep = lv.highlighted_child.name if lv.highlighted_child else None
        await lv.clear()

        fresh_list = await asyncio.to_thread(list_instances)

        # key by manager:name — the same name can exist under both managers
        statuses = {}
        for inst in fresh_list:
            key = f"{inst['manager']}:{inst['name']}"
            statuses[key] = await asyncio.to_thread(_compute_status, inst)

        # running first, then a failure state, then a clean stop
        fresh_list.sort(key=lambda inst: _STATUS_ORDER.get(statuses[f"{inst['manager']}:{inst['name']}"], 1))

        self._instances = {f"{inst['manager']}:{inst['name']}": inst for inst in fresh_list}
        self._row_owner = {}
        self._row_db = {}
        keys, items = [], []
        name_width = self._name_width()
        uptime_width = self._uptime_width()

        for inst in fresh_list:
            key = f"{inst['manager']}:{inst['name']}"
            self._instance_status[key] = statuses[key]
            self._row_owner[key] = key
            items.append(ListItem(Label(self._render_instance_row(inst, statuses[key])), name=key))
            keys.append(key)

            # every instance's dbs are shown nested under it, not just the
            # highlighted one, so this fetches them all upfront
            names, port = await asyncio.to_thread(databases_of, inst)
            for db in names:
                db_key = f"{key}::db::{db}"
                self._row_owner[db_key] = key
                self._row_db[db_key] = db
                label = f"  [dim]└──[/] {_db_label(db, port, name_width, uptime_width, indent=4)}"
                items.append(ListItem(Label(label), name=db_key))
                keys.append(db_key)

        if items:
            # await the mounts, else setting index races the append and the
            # highlight bar lands on nothing
            await lv.extend(items)
            lv.index = keys.index(keep) if keep in keys else 0

    def _name_width(self) -> int:
        """Name column width, sized to the longest instance currently shown
        (some real unit names run past the old fixed 24, which misaligned
        every row's uptime/status against a longer neighbour)."""
        if not self._instances:
            return 24
        return max(24, max(len(_display_name(inst)) for inst in self._instances.values()))

    def _uptime_width(self) -> int:
        """Uptime column width, sized to the longest uptime currently shown.

        `format_duration`'s `<D>d HH:MM:SS` grows past a fixed width once an
        instance has been up for days — a hardcoded width just misaligned
        the db rows' port column against it once that happened.
        """
        if not self._instances:
            return 10
        return max(10, max(len(inst["uptime"]) for inst in self._instances.values()))

    def _render_instance_row(self, inst: dict, status: str) -> str:
        dot = self._dot(status)
        color = {"running": "green", "stopped": "dim"}.get(status, "red")
        width = self._name_width()
        uptime_width = self._uptime_width()
        return f"{dot} {_display_name(inst):<{width}} {inst['uptime']:>{uptime_width}}  [{color}]{status.upper()}[/]"

    def _dot(self, status: str) -> str:
        if status == "stopped":
            return "○"
        if status == "running":
            return "[green]●[/]" if self._pulse_on else " "
        return "[red]●[/]"  # failed / exited / fatal

    def _pulse_running(self) -> None:
        """Fade the running dot in/out in place — a cheap re-render off the
        cached state, no process polling (that's poll_instances' job)."""
        self._pulse_on = not self._pulse_on
        for item in self.query_one("#instances", ListView).children:
            inst = self._instances.get(item.name or "")
            if inst is None:  # a db row, not an instance row
                continue

            label = next(iter(item.query(Label)), None)
            if label is not None:
                label.update(self._render_instance_row(inst, self._instance_status.get(item.name or "", "stopped")))

    def poll_instances(self) -> None:
        """Refresh the running marks in place so an external start/stop shows up.

        Rebuilds the list only when the set of instances changes — otherwise it
        just re-labels, leaving selection, the db rows and the log/top views
        untouched.
        """
        self._poll_instances()

    @work(exclusive=True, group="instances")
    async def _poll_instances(self) -> None:
        fresh_list = await asyncio.to_thread(list_instances)
        fresh = {f"{i['manager']}:{i['name']}": i for i in fresh_list}
        if set(fresh) != set(self._instances):
            self.refresh_instances()
            return

        self._instances = fresh
        for item in self.query_one("#instances", ListView).children:
            inst = fresh.get(item.name or "")
            if inst is not None:
                self._instance_status[item.name or ""] = await asyncio.to_thread(_compute_status, inst)

    def current_instance(self) -> dict | None:
        item = self.query_one("#instances", ListView).highlighted_child
        if item is None or item.name is None:
            return None

        owner = self._row_owner.get(item.name)
        return self._instances.get(owner) if owner else None

    def highlighted_db(self) -> tuple[dict, str] | None:
        """(instance, db name) if a db row is highlighted, else None."""
        item = self.query_one("#instances", ListView).highlighted_child
        if item is None or item.name is None:
            return None

        db = self._row_db.get(item.name)
        if db is None:
            return None

        inst = self._instances.get(self._row_owner[item.name])
        return (inst, db) if inst else None

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = self.query_one("#instances", ListView).highlighted_child
        key = item.name if item is not None else None

        if key != self._shown_key:
            self._shown_key = key
            hit = self.highlighted_db()

            if hit is not None:
                inst, db = hit
                self.query_one(ActivityPane).show_database(inst, db)
            else:
                self.query_one(ActivityPane).show_instance(self.current_instance())

        self.refresh_bindings()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # start/stop/restart only make sense on the instances pane, so their
        # footer entries appear/disappear as focus moves (see check_action)
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # False hides the footer key entirely (None just dims it — Textual's
        # Screen.active_bindings skips on `is False`, not falsy)
        if action in ("toggle_start_stop", "restart"):
            focused = self.focused
            return bool(focused is not None and focused.id == "instances")

        # a tab shortcut only makes sense if its name is one of the current
        # mode's tabs (instance-mode, db-mode); "l"/"c" each bind two names,
        # gated here so only the active one shows/fires
        if action == "select_tab":
            (name,) = parameters
            return self.query_one(ActivityPane).has_tab(str(name))

        if action == "search":
            return self.query_one(ActivityPane).has_search()

        if action in ("kill_process", "quit_process"):
            return self.query_one(ActivityPane).is_processes_active()

        if action == "dumpstacks":
            return self.current_instance() is not None

        if action == "toggle_config_mode":
            return self.query_one(ActivityPane).is_config_active()

        return True

    def action_prev_tab(self) -> None:
        self.query_one(ActivityPane).prev_tab()
        self.refresh_bindings()

    def action_next_tab(self) -> None:
        self.query_one(ActivityPane).next_tab()
        self.refresh_bindings()

    def action_select_tab(self, name: str) -> None:
        self.query_one(ActivityPane).select_tab_by_name(name)
        self.refresh_bindings()

    def action_search(self) -> None:
        self.query_one(ActivityPane).open_search()

    def action_toggle_maximize(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
        elif self.focused is not None:
            self.screen.maximize(self.focused)

    def action_toggle_start_stop(self) -> None:
        inst = self.current_instance()
        if inst is None:
            return

        running = self._instance_status.get(f"{inst['manager']}:{inst['name']}") == "running"
        self._instance_action("stop" if running else "start")

    def action_restart(self) -> None:
        self._instance_action("restart")

    def _instance_action(self, action: str) -> None:
        inst = self.current_instance()
        if inst is None:
            return

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._run_instance_action(action)

        self.push_screen(
            ConfirmScreen(f"{action.capitalize()} {inst['name']} ({inst['manager']})?"),
            on_confirm,
        )

    @work(exclusive=True, group="instance-action")
    async def _run_instance_action(self, action: str) -> None:
        inst = self.current_instance()
        if inst is None:
            return

        name, manager = inst["name"], inst["manager"]
        error = await asyncio.to_thread(instance_action, name, action, manager)
        if error:
            self.app.notify(error, severity="warning", timeout=3)
        self.poll_instances()  # re-label in place; keeps selection, no flicker

    def action_kill_process(self) -> None:
        proc = self.query_one(ActivityPane).selected_process()
        if proc is None or proc.get("kind") == "pg":
            return

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                signal_process(proc["pid"], signal.SIGKILL)

        self.push_screen(ConfirmScreen(f"Kill PID {proc['pid']}?"), on_confirm)

    def action_quit_process(self) -> None:
        """Send SIGQUIT (kill -3) — the process dumps a traceback to its
        logfile — then jump to Logs so the dump is visible right away.

        Odoo rows only: a postgres backend isn't ours to signal directly
        (use the DB tools' own termination, not SIGQUIT/SIGKILL)."""
        proc = self.query_one(ActivityPane).selected_process()
        if proc is None or proc.get("kind") == "pg":
            return

        signal_process(proc["pid"], signal.SIGQUIT)
        self.query_one(ActivityPane).select_tab_by_name("Logs")

    def action_toggle_config_mode(self) -> None:
        self.query_one(ActivityPane).toggle_config_mode()

    def action_dumpstacks(self) -> None:
        inst = self.current_instance()
        if inst is None:
            return
        self._run_dumpstacks(inst)

    @work(exclusive=True, group="dumpstacks")
    async def _run_dumpstacks(self, inst: dict) -> None:
        """Trigger a stack dump, then jump to Logs to see it — same pattern
        as action_quit_process's local SIGQUIT.
        """
        out = await asyncio.to_thread(dump_all_stacks, inst)

        if out:
            self.app.notify(out, timeout=3)
        self.query_one(ActivityPane).select_tab_by_name("Logs")


def run() -> None:
    OdooActivity().run()
