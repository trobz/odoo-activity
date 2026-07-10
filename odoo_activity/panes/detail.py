"""ActivityPane — Tabbed view for the selected row.

Switches dynamically based on selection: Instance mode shows
Processes/Logs/Config, while Database mode shows db-specific tabs. Mode-switched
inline, no popups.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
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
    configfile_of,
    db_port_of,
    instance_procs,
    instance_version,
    logfile_of,
    long_queries,
    odoo_pid_for_port,
    parse_odoo_db_output,
    pg_client_port,
    proc_cpu_ticks,
    render_config,
    start_odoo_db,
    stringify,
    table_columns,
    tail,
)

if TYPE_CHECKING:
    from odoo_activity.tui import OdooActivity


def _inst_key(inst: dict | None) -> str | None:
    return f"{inst['manager']}:{inst['name']}" if inst else None


class _DbTab:
    """The pane's one db-tab fetch/result — one pane shows one db tab at a
    time, so there's never more than one to track."""

    def __init__(self) -> None:
        self.ident: tuple[str, str] | None = None  # (category, db) most recently requested
        self.proc: subprocess.Popen[str] | None = None  # its still-running odoo-db, if any
        self.rows: list[dict] = []  # raw (untruncated) rows behind #actable, outlives the fetch

    def abandon(self) -> None:
        """Kill a still-running fetch and forget it. Its process (and the
        query on Postgres's side) may keep running for a while regardless —
        see start_odoo_db — this only stops us from waiting on it."""
        if self.proc is not None:
            self.proc.kill()
            self.proc = None

        # Clearing `ident` forces `_fetch_db_tab` to reject stale results,
        # preventing them from overwriting the active view.
        self.ident = None


class ActivityTab(Static):
    """A clickable tab in ActivityPane's header."""

    def __init__(self, label: str, index: int, active: bool) -> None:
        super().__init__(label)
        self.index = index
        self.set_class(active, "-active")

    def on_click(self) -> None:
        self.app.query_one(ActivityPane).select_tab(self.index)


class _RawScroll(VerticalScroll):
    """VerticalScroll (like every Container) hardcodes ALLOW_MAXIMIZE = False,
    which would block `f` from walking up to the pane while this is
    focused — opt back in."""

    ALLOW_MAXIMIZE = True


class ActivityPane(Vertical):
    """Mode-switched tab view: the instance-mode tabs for the highlighted
    instance, or the db-mode tabs for the highlighted database."""

    DEFAULT_CSS = """
    ActivityPane { border: round $accent; background: transparent; }
    ActivityPane:focus-within { border: round $primary; }
    #actabs { height: 1; margin-bottom: 1; padding: 0 1; }
    ActivityTab { width: auto; padding: 0 1; color: $text-muted; }
    ActivityTab:hover { color: $text; }
    ActivityTab.-active { background: $primary; color: $text; text-style: bold; }
    #acbody { background: transparent; }
    #actable { background: transparent; display: none; }
    #acsearch { margin-bottom: 1; }
    #acraw { background: transparent; display: none; }
    """

    # ALLOW_MAXIMIZE makes maximizing a focused child (DataTable/Log/Input)
    # maximize the pane instead of just that child.
    ALLOW_MAXIMIZE = True

    CONFIG_MODES: ClassVar = ["compact", "explain", "expand", "clean"]

    TABS: ClassVar = {
        "instance": ["Processes", "Logs", "Config"],
        "database": ["Queries", "Users", "Locks", "Jobs", "Crons", "Modules"],
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
        yield Log(id="acbody", highlight=True)
        yield DataTable(id="actable", zebra_stripes=True, cursor_type="row")
        with _RawScroll(id="acraw"):
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
        self._proc_rows: list[dict] = []  # rows behind #actable in the Processes tab
        self._config_mode = self.CONFIG_MODES[0]  # which odoo-config view the Config tab shows
        self._inflight: dict[str, object] = {}  # key -> ident of the run in progress
        self._pending: dict[str, tuple[object, Callable[[], Awaitable[None]]]] = {}
        self._dbtab = _DbTab()
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

    def is_config_active(self) -> bool:
        return self._mode == "instance" and self.TABS["instance"][self._tab] == "Config"

    def is_processes_active(self) -> bool:
        return self._mode == "instance" and self.TABS["instance"][self._tab] == "Processes"

    def has_search(self) -> bool:
        """Logs and Config both render plain text into #acbody with the same
        substring filter (see _render_log)."""
        return self.is_logs_active() or self.is_config_active()

    def selected_process(self) -> dict | None:
        """The process under the Processes tab's table cursor, if any."""
        if not self.is_processes_active() or not self._proc_rows:
            return None

        idx = self.query_one("#actable", DataTable).cursor_row
        return self._proc_rows[idx] if 0 <= idx < len(self._proc_rows) else None

    def open_search(self) -> None:
        if not self.has_search():
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

        if self.is_processes_active():
            self.run_worker(self._jump_from_process_row(idx))
            return

        # only db-mode tabs drill into raw json
        if self._mode != "database":
            return

        if idx < len(self._dbtab.rows):
            self._show_raw(self._dbtab.rows[idx])

    async def _jump_from_process_row(self, idx: int) -> None:
        """`enter` on a postgres row moves the cursor to the Odoo worker
        driving it, traced via the backend's client port (see
        probes.odoo_pid_for_port). A no-op on an Odoo row, or when nothing
        in this table matches (a different instance's worker, a unix-socket
        connection with no port, `lsof` missing)."""
        if not (0 <= idx < len(self._proc_rows)):
            return

        row = self._proc_rows[idx]
        if row.get("kind") != "pg":
            return

        port = pg_client_port(row["cmd"])
        target = await asyncio.to_thread(odoo_pid_for_port, port) if port else None
        row_idx = next((i for i, p in enumerate(self._proc_rows) if p["pid"] == target), None)

        if row_idx is None:
            self.app.notify("No matching Odoo process found", severity="warning", timeout=2)
            return

        self.query_one("#actable", DataTable).move_cursor(row=row_idx)

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
        """Switch to instance mode for `inst`."""
        self._dbtab.abandon()  # leaving database mode; don't leave a fetch running unseen
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
        """Keep Processes live while it's the active tab. Called on the host
        refresh timer."""
        if self.is_processes_active():
            self._render_processes()

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
            body = self.query_one("#acbody", Log)
            # only follow the tail if the user was already at the bottom —
            # else a scroll-up to read older lines gets yanked back down
            # every time new data lands
            body.write(data, scroll_end=self._at_bottom(body))

    def _render_mode(self) -> None:
        tabs = self.TABS[self._mode]
        if self._mode != self._tabs_mode:
            self._tabs_mode = self._mode
            self._tab = 0
            self._build_tabs(tabs)

        self._render_active()

    def _title(self) -> str:
        title = self.MODE_TITLE[self._mode]
        if self.is_config_active():
            return f"{title} — Config ({self._config_mode})"
        return title

    def _build_tabs(self, names: list[str]) -> None:
        strip = self.query_one("#actabs", Horizontal)
        strip.remove_children()
        strip.mount_all(ActivityTab(name, i, active=(i == self._tab)) for i, name in enumerate(names))

    def _render_active(self) -> None:
        tabs = self.TABS[self._mode]

        self._tab %= len(tabs)
        for tab in self.query(ActivityTab):
            tab.set_class(tab.index == self._tab, "-active")
        self.border_title = self._title()

        active = tabs[self._tab]
        self._log_path = None
        self.query_one("#acsearch", Input).display = False

        if self._mode == "instance":
            if active == "Logs":
                self._use("log")
                self._load_log(self._instance)
            elif active == "Config":
                self._use("log")
                self._render_config()
            else:  # Processes
                self._use("table")
                self._top_prev = {}
                self._render_processes()
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
        was_at_bottom = self._at_bottom(body)
        body.clear()

        if not self._log_query:
            text = self._log_text
        else:
            needle = self._log_query.lower()
            lines = [ln for ln in self._log_text.splitlines() if needle in ln.lower()]
            text = "\n".join(lines) if lines else f"(no match: {self._log_query})"

        # Tail logs stick to the bottom only if already there (a fresh,
        # still-empty widget counts as "at bottom" so a first load still
        # opens tailing); the static config always opens at the top
        body.write(text, scroll_end=self.is_logs_active() and was_at_bottom)
        if not self.is_logs_active():
            body.scroll_home(animate=False)

    @staticmethod
    def _at_bottom(body: Log) -> bool:
        return body.scroll_y >= body.max_scroll_y - 1

    def toggle_config_mode(self) -> None:
        """Cycle the Config tab through odoo-config's views: compact (only
        non-default keys), explain (those same keys plus help + default,
        the "exhaustive" one), expand (every valid option filled in) and
        clean (drops anything unknown to the schema or invalid for the
        version/edition)."""
        if not self.is_config_active():
            return

        idx = (self.CONFIG_MODES.index(self._config_mode) + 1) % len(self.CONFIG_MODES)
        self._config_mode = self.CONFIG_MODES[idx]
        self.border_title = self._title()
        self.app.notify(f"Config mode: {self._config_mode}", timeout=2)
        self._render_config()

    def _render_config(self) -> None:
        self._log_body(f"Loading {self._config_mode}…")  # else the prior tab/mode's text lingers until the fetch lands
        self._coalesce("config", (_inst_key(self._instance), self._config_mode), self._do_render_config)

    async def _do_render_config(self) -> None:
        inst = self._instance
        if inst is None:
            self._show_config_text("(no instance)")
            return

        config = await asyncio.to_thread(configfile_of, inst)
        if config is None:
            self._show_config_text("(no config file found)")
            return

        version = await asyncio.to_thread(instance_version, inst)
        text = await asyncio.to_thread(render_config, config, version, self._config_mode)
        self._show_config_text(text)

    def _show_config_text(self, text: str) -> None:
        # not a tailed file, so #log_path stays None — poll() then leaves it alone
        self._log_path = None
        self._log_query = None
        self._log_text = text
        self._render_log()

    def _render_processes(self) -> None:
        self._coalesce("processes", _inst_key(self._instance), self._do_render_processes)

    async def _do_render_processes(self) -> None:
        inst = self._instance
        if inst is None:
            self._proc_rows = []
            self._show_process_table([])
            return

        odoo_procs, pg_procs = await asyncio.to_thread(instance_procs, inst)

        if self._instance is not inst or not self.is_processes_active():
            return  # instance or tab changed while this was fetching; the result is stale

        procs = [{**p, "kind": "odoo"} for p in odoo_procs] + [{**p, "kind": "pg"} for p in pg_procs]
        now = time.monotonic()
        prev, self._top_prev = self._top_prev, {}
        rows = []

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

            rows.append({**p, "time": time_str, "cpu": f"{cpu:.1f}"})

        # sort by cpu load desc, in order to quickly spot the ones that matters
        rows.sort(key=lambda p: float(p["cpu"]), reverse=True)
        self._proc_rows = rows
        self._show_process_table(rows)

    def _show_process_table(self, rows: list[dict]) -> None:
        """Populate the DataTable, preserving the user's selected PID and
        scroll position across refreshes."""
        table = self.query_one("#actable", DataTable)

        keep_pid = table.get_row_at(table.cursor_row)[0] if table.row_count else None
        # clear(columns=True) below wipes both scroll axes too, not just the
        # rows — without saving them here, the 1s tick would yank a
        # manually-scrolled (horizontally or vertically) view back to the
        # top-left corner on every refresh
        scroll_x, scroll_y = table.scroll_x, table.scroll_y

        table.clear(columns=True)
        table.add_columns("PID", "PPID", "USER", "TIME", "CPU%", "MEM%", "COMMAND")
        for i, p in enumerate(rows):
            table.add_row(p["pid"], p["ppid"], p["user"], p["time"], p["cpu"], p["mem"], p["cmd"], key=str(i))

        if not rows:
            return

        # Find where the old PID moved to, default to row 0 if missing
        restore = next((i for i, p in enumerate(rows) if p["pid"] == keep_pid), 0)
        table.move_cursor(row=restore)
        table.scroll_to(x=scroll_x, y=scroll_y, animate=False)

    def _load_db_tab(self, category: str) -> None:
        self._showing_raw = False
        self._log_body(f"Loading {category.lower()}…")  # clear any prior tab's table while this one loads

        if self._db is None:
            self._log_body("(no database)")
            return

        _inst, db = self._db
        ident = (category, db)
        if ident == self._dbtab.ident and self._dbtab.proc is not None:
            return  # already fetching this exact tab; let it finish rather than restart

        self._dbtab.abandon()  # drop the previous tab's client (see start_odoo_db)
        self._dbtab.ident = ident
        self.run_worker(self._fetch_db_tab(category, db, ident), group="dbtab")

    async def _fetch_db_tab(self, category: str, db: str, ident: tuple[str, str]) -> None:
        port = db_port_of(self._db[0]) if self._db else None

        if category == "Queries":
            # see `long_queries`
            rows = await asyncio.to_thread(long_queries, db, port)
            if ident != self._dbtab.ident:
                return

            if not rows:
                self._log_body("(empty)")
            else:
                self._dbtab.rows = rows
                self._show_datatable(rows)
            return

        proc = await asyncio.to_thread(start_odoo_db, category.lower(), db, port)
        self._dbtab.proc = proc

        def _wait() -> tuple[str, str] | None:
            try:
                return proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:  # backstop for a genuinely stuck call
                proc.kill()
                return None

        result = await asyncio.to_thread(_wait)

        if ident != self._dbtab.ident:
            return  # superseded by a newer tab selection; this result is stale

        self._dbtab.proc = None

        if result is None:
            self._log_body("(odoo-db timed out after 90s)")
            return

        rows, raw = parse_odoo_db_output(*result)
        if rows is None:
            self._log_body(raw or "(no output)")
        elif not rows:
            self._log_body("(empty)")
        else:
            self._dbtab.rows = rows
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
