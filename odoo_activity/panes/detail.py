"""ActivityPane — the tabbed view for whichever row is highlighted.

The app just tells it what's selected (an instance, or one of its
databases); this pane decides how to show it, switching a tab strip over a
Log (text) and a DataTable. Instance mode shows Top/Logs; database mode
shows its db-mode tabs — one pane, mode-switched, no popups.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.syntax import Syntax
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Log, Static

from odoo_activity.probes import (
    CLK_TCK,
    db_port_of,
    logfile_of,
    odoo_db_rows,
    proc_cpu_ticks,
    procs_of,
    stringify,
    table_columns,
    tail,
)

if TYPE_CHECKING:
    from odoo_activity.tui import OdooActivity


def _inst_key(inst: dict | None) -> str | None:
    return f"{inst['manager']}:{inst['name']}" if inst else None


class ActivityTab(Static):
    """A clickable tab in ActivityPane's header."""

    def __init__(self, label: str, index: int, active: bool) -> None:
        super().__init__(label)
        self.index = index
        self.set_class(active, "-active")

    def on_click(self) -> None:
        self.app.query_one(ActivityPane).select_tab(self.index)


class ActivityPane(Vertical):
    """Mode-switched tab view: Top/Logs for the highlighted instance, or
    the db-mode tabs for the highlighted database."""

    DEFAULT_CSS = """
    ActivityPane { border: round $primary; background: transparent; }
    ActivityPane:focus-within { border: round $accent; }
    #actabs { height: 1; margin-bottom: 1; padding: 0 1; }
    ActivityTab { width: auto; padding: 0 1; color: $text-muted; }
    ActivityTab:hover { color: $text; }
    ActivityTab.-active { background: $primary; color: $text; text-style: bold; }
    #acbody { background: transparent; }
    #actable { background: transparent; display: none; }
    #acsearch { margin-bottom: 1; }
    #acraw { background: transparent; display: none; }
    """

    TABS: ClassVar = {
        "instance": ["Top", "Logs"],
        "database": ["Users", "Locks", "Jobs", "Crons", "Modules", "Stats"],
    }
    MODE_TITLE: ClassVar = {"instance": "Instance", "database": "Database"}

    if TYPE_CHECKING:

        @property
        def app(self) -> OdooActivity: ...

    def compose(self) -> ComposeResult:
        yield Horizontal(id="actabs")
        yield Input(
            id="acsearch",
            placeholder="search logs, enter to apply, empty clears",
            compact=True,
        )
        yield Log(id="acbody", highlight=False)
        yield DataTable(id="actable", zebra_stripes=True, cursor_type="row")
        with VerticalScroll(id="acraw"):
            yield Static(id="acraw-body")

    def on_mount(self) -> None:
        self._mode = "instance"
        self._tabs_mode: str | None = None  # tab set currently built in #actabs
        self._tab = 0
        self._instance: dict | None = None
        self._db: tuple[dict, str] | None = None  # (instance, db name) in database mode
        self._log_path: Path | None = None
        self._log_pos = 0
        self._log_text = ""  # full text currently loaded/followed, for re-filtering
        self._log_query: str | None = None
        self._top_prev: dict[str, tuple[int, float]] = {}  # pid -> (ticks, monotonic)
        self._inflight: dict[str, object] = {}  # key -> ident of the run in progress
        self._pending: dict[str, tuple[object, Callable[[], Awaitable[None]]]] = {}
        self._dbtab_rows: list[dict] = []  # raw (untruncated) rows behind #actable
        self._showing_raw = False  # viewing one row's raw json in #acbody
        self._render_mode()

    def _coalesce(self, key: str, ident: object, factory: Callable[[], Awaitable[None]]) -> None:
        """Run at most one task per `key` at a time, identified by `ident`.

        `asyncio.to_thread` can't interrupt a subprocess/file read that's
        already started, so a retrigger while one is still running doesn't
        stop it — it just leaves it running unseen while a duplicate starts
        alongside it. Queuing the latest call instead (dropping any earlier
        one still waiting) means only one `key` is ever actually in flight.

        If the request coming in matches the one already running (e.g. the
        user tabs away and back before it finishes), drop any queued
        follow-up rather than re-running it once the in-flight call
        finishes — its result already answers this request.
        """
        if key in self._inflight:
            if self._inflight[key] == ident:
                self._pending.pop(key, None)
            else:
                self._pending[key] = (ident, factory)
            return

        self._inflight[key] = ident
        self.run_worker(self._run_coalesced(key, factory), group=key, exclusive=True)

    async def _run_coalesced(self, key: str, factory: Callable[[], Awaitable[None]]) -> None:
        try:
            await factory()
        finally:
            del self._inflight[key]
            nxt = self._pending.pop(key, None)
            if nxt is not None:
                ident, next_factory = nxt
                self._coalesce(key, ident, next_factory)

    def is_logs_active(self) -> bool:
        return self._mode == "instance" and self.TABS["instance"][self._tab] == "Logs"

    def open_search(self) -> None:
        if not self.is_logs_active():
            return

        box = self.query_one("#acsearch", Input)
        box.value = ""  # blank each time: enter alone clears an existing filter
        box.display = True
        box.focus()

    def on_key(self, event: events.Key) -> None:
        box = self.query_one("#acsearch", Input)
        if event.key == "escape" and box.has_focus:
            box.display = False
            self.app.query_one("#instances").focus()
            event.stop()
            return

        if event.key == "escape" and self._showing_raw:
            self._showing_raw = False
            self._use("table")
            event.stop()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return

        idx = int(event.row_key.value)
        if idx < len(self._dbtab_rows):
            self._show_raw(self._dbtab_rows[idx])

    def _show_raw(self, row: dict) -> None:
        self._showing_raw = True
        self._use("raw")
        text = json.dumps(row, indent=2, default=str)
        theme = "ansi_dark" if self.app.current_theme.dark else "ansi_light"
        syntax = Syntax(text, "json", theme=theme, background_color="default", word_wrap=True)
        self.query_one("#acraw-body", Static).update(syntax)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "acsearch":
            return

        self._log_query = event.value.strip() or None
        event.input.display = False
        self.app.query_one("#instances").focus()
        self._render_log()

    def show_instance(self, inst: dict | None) -> None:
        """Switch to instance mode (Top/Logs) for `inst`."""
        self._mode = "instance"
        self._instance = inst
        self._render_mode()

    def show_database(self, inst: dict, db: str) -> None:
        """Switch to database mode for `db`."""
        self._mode = "database"
        self._db = (inst, db)
        self._render_mode()

    def select_tab(self, index: int) -> None:
        self._tab = index
        self._render_active()

    def select_tab_by_name(self, name: str) -> None:
        tabs = self.TABS[self._mode]
        if name in tabs:
            self.select_tab(tabs.index(name))

    def has_tab(self, name: str) -> bool:
        """True if `name` is one of the current mode's tabs."""
        return name in self.TABS[self._mode]

    def prev_tab(self) -> None:
        self.select_tab(self._tab - 1)

    def next_tab(self) -> None:
        self.select_tab(self._tab + 1)

    def tick(self) -> None:
        """Keep Top live while it's the active tab. Called on the host refresh timer."""
        if self._mode == "instance" and self.TABS["instance"][self._tab] == "Top":
            self._render_top()

    def poll(self) -> None:
        """Append newly-written log lines while Logs is the active tab. Called
        on a timer; a no-op whenever another tab is active (``_log_path`` is
        None then)."""
        if self._log_path is None:
            return

        try:
            size = self._log_path.stat().st_size
        except OSError:
            return

        if size < self._log_pos:  # rotated/truncated
            self._log_pos = 0
        if size == self._log_pos:
            return

        with self._log_path.open() as f:
            f.seek(self._log_pos)
            data = f.read()
            self._log_pos = f.tell()

        if not data:
            return

        self._log_text += data
        if self._log_query:
            self._render_log()
        else:
            self.query_one("#acbody", Log).write(data)

    def _render_mode(self) -> None:
        self.border_title = self._title()

        tabs = self.TABS[self._mode]
        if self._mode != self._tabs_mode:
            self._tabs_mode = self._mode
            self._tab = 0
            self._build_tabs(tabs)

        self._render_active()

    def _title(self) -> str:
        return f"{self.MODE_TITLE[self._mode]}"

    def _build_tabs(self, names: list[str]) -> None:
        strip = self.query_one("#actabs", Horizontal)
        strip.remove_children()
        strip.mount_all(ActivityTab(name, i, active=(i == self._tab)) for i, name in enumerate(names))

    def _render_active(self) -> None:
        tabs = self.TABS[self._mode]

        self._tab %= len(tabs)
        for tab in self.query(ActivityTab):
            tab.set_class(tab.index == self._tab, "-active")

        active = tabs[self._tab]
        self._log_path = None
        self.query_one("#acsearch", Input).display = False

        if self._mode == "instance":
            self._use("log")
            if active == "Logs":
                self._load_log(self._instance)
            else:  # Top
                self._top_prev = {}
                self._render_top()
        else:
            self._load_db_tab(active)

    def _load_log(self, inst: dict | None) -> None:
        self._coalesce("log", _inst_key(inst), lambda: self._do_load_log(inst))

    async def _do_load_log(self, inst: dict | None) -> None:
        path = await asyncio.to_thread(logfile_of, inst) if inst else None
        text = await asyncio.to_thread(tail, path) if path is not None else None
        self._follow_log(path, text)

    def _follow_log(self, path: Path | None, text: str | None) -> None:
        self._log_path = path
        self._log_query = None

        if path is None:
            self._log_pos = 0
            self._log_text = "(no logfile configured)"
            self._render_log()
            return

        self._log_text = text or ""
        self._render_log()

        try:
            self._log_pos = path.stat().st_size
        except OSError:
            self._log_pos = 0

    def _render_log(self) -> None:
        body = self.query_one("#acbody", Log)
        body.clear()

        if not self._log_query:
            body.write(self._log_text)
            return

        needle = self._log_query.lower()
        lines = [ln for ln in self._log_text.splitlines() if needle in ln.lower()]
        body.write("\n".join(lines) if lines else f"(no match: {self._log_query})")

    def _render_top(self) -> None:
        self._coalesce("top", _inst_key(self._instance), self._do_render_top)

    async def _do_render_top(self) -> None:
        inst = self._instance
        body = self.query_one("#acbody", Log)
        if inst is None:
            body.clear()
            body.write("(no instance)")
            return

        procs = await asyncio.to_thread(procs_of, inst)
        now = time.monotonic()
        prev, self._top_prev = self._top_prev, {}
        lines = [
            f"{'PID':>7} {'PPID':>7} {'USER':<9} {'TIME':>9} {'CPU%':>5} {'MEM%':>5}  COMMAND",
        ]

        for p in procs:
            pid = p["pid"]
            ticks = proc_cpu_ticks(pid)
            cpu = 0.0
            time_str = "-"

            if ticks is not None:
                self._top_prev[pid] = (ticks, now)
                secs = int(ticks / CLK_TCK)  # cumulative CPU time (top's TIME+)
                time_str = f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"

                if pid in prev and (dt := now - prev[pid][1]) > 0:
                    cpu = max(0.0, ticks - prev[pid][0]) / CLK_TCK / dt * 100

            lines.append(f"{pid:>7} {p['ppid']:>7} {p['user']:<9} {time_str:>9} {cpu:5.1f} {p['mem']:>5}  {p['cmd']}")

        if not procs:
            lines.append("(no running processes)")

        body.clear()
        body.write("\n".join(lines))

    def _load_db_tab(self, category: str) -> None:
        self._showing_raw = False
        self._log_body(f"Loading {category.lower()}…")  # clear any prior tab's table while this one loads

        if self._db is None:
            self._log_body("(no database)")
            return

        _inst, db = self._db
        self._coalesce("dbtab", (category, db), lambda: self._fetch_db_tab(category, db))

    async def _fetch_db_tab(self, category: str, db: str) -> None:
        port = db_port_of(self._db[0]) if self._db else None
        rows, raw = await asyncio.to_thread(odoo_db_rows, category.lower(), db, port)

        if rows is None:
            self._log_body(raw or "(no output)")
        elif not rows:
            self._log_body("(empty)")
        else:
            self._dbtab_rows = rows
            self._show_datatable(rows)

    def _use(self, which: str) -> None:
        """Show the Log, the DataTable, or the raw-json view in the pane body."""
        self.query_one("#acbody", Log).display = which == "log"
        self.query_one("#actable", DataTable).display = which == "table"
        self.query_one("#acraw", VerticalScroll).display = which == "raw"

    def _show_datatable(self, rows: list[dict]) -> None:
        table = self.query_one("#actable", DataTable)
        table.clear(columns=True)
        columns = table_columns(rows)
        table.add_columns(*(c.upper() for c in columns))

        for i, row in enumerate(rows):
            table.add_row(*(stringify(row.get(c, "")) for c in columns), key=str(i))

        self._use("table")

    def _log_body(self, text: str) -> None:
        self._use("log")
        body = self.query_one("#acbody", Log)
        body.clear()
        body.write(text)
