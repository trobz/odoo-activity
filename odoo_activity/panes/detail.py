"""ActivityPane — Tabbed view for the selected row.

Switches dynamically based on selection: Instance mode shows instance-specific
tabs, while Database mode shows db-specific tabs. Mode-switched inline, no
popups.
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, RichLog, Static, Tree
from textual.widgets.data_table import RowDoesNotExist

from odoo_activity.host import Host, to_thread
from odoo_activity.panes.confirm import ConfirmScreen
from odoo_activity.panes.processes import render_processes
from odoo_activity.panes.stacks import filter_workers, render_stacks
from odoo_activity.probes import (
    ALL_ROW_FLAGS,
    CLK_TCK,
    Instance,
    ProcRow,
    Worker,
    _read_new_bytes,
    configfile_of,
    db_port_of,
    instance_pid,
    instance_procs,
    instance_version,
    instance_workers,
    job_groups,
    jobrunner_pids,
    jobs_in_group,
    logfile_of,
    long_queries,
    odoo_pid_for_port,
    parse_odoo_db_output,
    pg_client_port,
    proc_cpu_ticks_many,
    render_config,
    requeue_jobs,
    row_matches,
    session_count,
    session_dir_of,
    shell_command,
    signal_process,
    start_odoo_db,
    stringify,
    table_columns,
    tail,
    try_local_clipboard,
)

if TYPE_CHECKING:
    from odoo_activity.tui import OdooActivity

_log = logging.getLogger("odoo_activity")


class ProcDisplayRow(ProcRow):
    """A `ProcRow` as shown in the Top tab: odoo vs. postgres, plus
    the CPU-time/percent computed each refresh from consecutive `ps` polls."""

    kind: str
    time: str
    cpu: str


def _inst_key(inst: Instance | None) -> str | None:
    return f"{inst['manager']}:{inst['name']}" if inst else None


# db-tab actions, as (id, label) -- the id is the Button's, dispatched by
# `on_button_pressed`. Tab-wide, not row-scoped: they act on the database,
# whatever the cursor happens to be on.
_REQUEUE_ACTION = ("requeue-jobs", "⟳  Requeue jobs")


def _first_line(text: str) -> str:
    """A postgres error's first line — the rest is the offending SQL echoed
    back with a caret, which is ours, not the user's, to read."""
    return text.strip().splitlines()[0] if text.strip() else ""


class _DbTab:
    """The pane's one db-tab fetch/result — one pane shows one db tab at a
    time, so there's never more than one to track."""

    def __init__(self) -> None:
        # (category, db, jobs group) most recently requested -- the group is
        # part of it because the two Jobs depths are two different fetches of
        # the same tab, and a stale one must not land on the other
        self.ident: tuple[str, str, tuple[str, str] | None] | None = None
        self.proc: subprocess.Popen[str] | None = None  # its still-running odoo-db, if any
        self.rows: list[dict] = []  # raw (untruncated) rows behind #actable, outlives the fetch
        self.no_all = False  # this host's odoo-db rejected --all (see _fetch_db_tab)
        self.actions: list[tuple[str, str]] = []  # (id, label) of the tab's own actions
        self.numbered = False  # prefix rows with a 1..n column, for counting long lists by eye

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
        pane = self.app.query_one(ActivityPane)
        pane.select_tab(self.index)
        pane.focus_active()


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
    #acstacks { background: transparent; display: none; }
    #acprocesses { background: transparent; display: none; }
    /* db-tab actions: a strip of buttons under the body, shown only for a
       tab that has any (see _render_actions) */
    #acactions { height: auto; display: none; padding-top: 1; }
    #acactions Button { margin-right: 2; }
    """

    # ALLOW_MAXIMIZE makes maximizing a focused child (DataTable/Log/Input)
    # maximize the pane instead of just that child.
    ALLOW_MAXIMIZE = True

    CONFIG_MODES: ClassVar = ["compact", "explain", "expand", "clean"]

    TABS: ClassVar = {
        "instance": ["Top", "Processes", "Stacks", "Logs", "Config", "Toolbox"],
        "database": ["Queries", "Users", "Locks", "Jobs", "Crons", "Modules", "Params"],
    }

    # (label, signal) -- an int sends that signal to the instance's master
    # pid; None/"count_sessions" are special-cased -- see _run_toolbox_tool.
    TOOLBOX_TOOLS: ClassVar = [
        ("Spin up worker (SIGTTIN)", signal.SIGTTIN),
        ("Spin down worker (SIGTTOU)", signal.SIGTTOU),
        ("Open shell (copy command)", None),
        ("Count sessions", "count_sessions"),
    ]
    MODE_TITLE: ClassVar = {"instance": "Instance", "database": "Database"}

    # Top's search fields -- deliberately not every column, or e.g. "1" would
    # match half the table through ppid/cpu/mem/time instead of a real pid.
    TOP_SEARCH_KEYS: ClassVar = ("pid", "user", "cmd")

    if TYPE_CHECKING:

        @property
        def app(self) -> OdooActivity: ...

    def compose(self) -> ComposeResult:
        yield Horizontal(id="actabs")
        yield Input(
            id="acsearch",
            placeholder="search, enter to apply, empty clears",
            compact=True,
        )
        yield RichLog(id="acbody", highlight=True, wrap=True)
        yield DataTable(id="actable", zebra_stripes=True, cursor_type="row")
        with _RawScroll(id="acraw"):
            yield Static(id="acraw-body")
        yield Tree("stacks", id="acstacks")
        yield Tree("processes", id="acprocesses")
        yield Horizontal(id="acactions")

    def on_mount(self) -> None:
        self._mode = "instance"
        self._tabs_mode: str | None = None  # tab set currently built in #actabs
        self._tab = 0
        self._instance: Instance | None = None
        self._db: tuple[Instance, str] | None = None  # (instance, db name) in database mode
        self._log_path: Path | None = None
        self._log_proc: subprocess.Popen | None = None  # remote-only: streaming `tail -f`
        self._config_path: Path | None = None
        self._log_pos = 0
        self._log_text = ""  # full text currently loaded/followed, for re-filtering
        self._filters: dict[str, str] = {}  # tab name -> its search query
        self._show_all = False  # db-tab: include rows the active/installed flag hides
        self._top_prev: dict[str, tuple[int, float]] = {}  # pid -> (ticks, monotonic)
        self._proc_rows: list[ProcDisplayRow] = []  # rows behind #actable in the Top tab
        self._config_mode = self.CONFIG_MODES[0]  # which odoo-config view the Config tab shows
        self._inflight: dict[str, object] = {}  # key -> ident of the run in progress
        self._pending: dict[str, tuple[object, Callable[[], Awaitable[None]]]] = {}
        self._dbtab = _DbTab()
        self._jobs_group: tuple[str, str] | None = None  # (function, state) the Jobs tab drilled into
        self._actions_shown: list[str] = []  # action ids currently mounted in #acactions
        self._showing_raw = False  # viewing one row's raw json in #acbody
        self._stacks_cache: dict[str, tuple[list[Worker], Path]] = {}  # instance key -> its last dump
        self.query_one("#acstacks", Tree).show_root = False
        self.query_one("#acprocesses", Tree).show_root = False
        self._render_mode()

    def on_unmount(self) -> None:
        # an ssh streaming child isn't reaped on drop -- kill it explicitly
        # so quitting the app doesn't leave a `tail -f` running on the remote.
        self._stop_log_stream()

    def on_resize(self, event: events.Resize) -> None:
        # COMMAND's width is derived from table.size, which is still 0 the
        # first time _render_top runs (on_mount fires before layout
        # has sized anything) — redraw once the real size lands instead of
        # leaving it wrapped narrow until the next 1s poll tick.
        if self.is_top_active():
            self._show_process_table()
        elif self._mode == "database" and self._dbtab.rows and not self._showing_raw:
            self._populate_datatable(self._visible_db_rows())

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
        # not exclusive: _inflight already admits one worker per key, so it
        # could only ever cancel one that has finished its work
        self.run_worker(self._run_coalesced(key, factory), group=key)

    async def _run_coalesced(self, key: str, factory: Callable[[], Awaitable[None]]) -> None:
        try:
            await factory()
        finally:
            del self._inflight[key]
            nxt = self._pending.pop(key, None)
            if nxt is not None:
                ident, next_factory = nxt
                self._coalesce(key, ident, next_factory)

    def _active_tab(self) -> str:
        """Name of the tab `_tab` points at.

        Wrapped, because `_tab` outlives a mode switch: database mode has more
        tabs than instance mode, so an index valid in one can be out of range
        in the other until `_render_active` normalises it the same way.
        """
        tabs = self.TABS[self._mode]
        return tabs[self._tab % len(tabs)]

    def is_instance_mode(self) -> bool:
        return self._mode == "instance"

    def is_logs_active(self) -> bool:
        return self._mode == "instance" and self._active_tab() == "Logs"

    def is_config_active(self) -> bool:
        return self._mode == "instance" and self._active_tab() == "Config"

    def is_top_active(self) -> bool:
        return self._mode == "instance" and self._active_tab() == "Top"

    def is_processes_active(self) -> bool:
        return self._mode == "instance" and self._active_tab() == "Processes"

    def is_stacks_active(self) -> bool:
        return self._mode == "instance" and self._active_tab() == "Stacks"

    def is_toolbox_active(self) -> bool:
        return self._mode == "instance" and self._active_tab() == "Toolbox"

    def is_jobs_active(self) -> bool:
        return self._mode == "database" and self._active_tab() == "Jobs"

    def has_search(self) -> bool:
        """Logs and Config render plain text into #acbody with a substring
        filter (see _render_log); every database table filters its rows the
        same way (see _visible_db_rows), as do Top (_visible_proc_rows) and
        Stacks (filter_workers)."""
        if self._mode == "database":
            return True

        return self.is_logs_active() or self.is_config_active() or self.is_top_active() or self.is_stacks_active()

    def _active_filter(self) -> str | None:
        """The active tab's search query, for the border-title hint (_title)
        -- Logs/Config excluded, since that hint already shows their log/config
        path there instead."""
        if self._mode == "database" or self.is_top_active() or self.is_stacks_active():
            return self._filters.get(self._active_tab())
        return None

    def _all_row_flag(self) -> str | None:
        """The status column `A` toggles on the active db tab, if it has one
        (see probes.ALL_ROW_FLAGS)."""
        if self._mode != "database":
            return None
        return ALL_ROW_FLAGS.get(self._active_tab().lower())

    def has_show_all(self) -> bool:
        """Whether the active tab has rows `A` can reveal."""
        return self._all_row_flag() is not None

    def toggle_show_all(self) -> None:
        if not self.has_show_all():
            return

        if self._dbtab.no_all:
            self.app.notify("This host's odoo-db can't list inactive rows", severity="warning")
            return

        self._show_all = not self._show_all
        self._show_datatable()

    def selected_process(self) -> ProcDisplayRow | ProcRow | None:
        """The process under the cursor of whichever tab shows processes:
        the Top tab's table, or the Processes tab's tree (whose leaves carry
        their row as node data — group nodes carry none, and select nothing).

        In the table, read through the row key, not the cursor's position:
        under a filter the two differ, and `K`/`L` signalling the wrong pid
        is unforgivable.
        """
        if self.is_processes_active():
            node = self.query_one("#acprocesses", Tree).cursor_node
            return node.data if node is not None else None

        if not self.is_top_active() or not self._proc_rows:
            return None

        table = self.query_one("#actable", DataTable)
        if not table.row_count:
            return None

        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        idx = int(key) if key is not None else -1
        return self._proc_rows[idx] if 0 <= idx < len(self._proc_rows) else None

    def open_search(self) -> None:
        if not self.has_search():
            return

        box = self.query_one("#acsearch", Input)
        box.value = ""  # blank each time: enter alone clears an existing filter
        box.placeholder = f"filter {self._active_tab().lower()}, enter to apply, empty clears"
        box.display = True
        box.focus()

    def on_key(self, event: events.Key) -> None:
        box = self.query_one("#acsearch", Input)
        if event.key == "escape" and box.has_focus:
            box.display = False
            self.focus_active()
            event.stop()
            return

        if event.key == "escape" and self._showing_raw:
            self._showing_raw = False
            self._use("table")
            event.stop()
            return

        if event.key == "escape" and self.is_jobs_active() and self._jobs_group is not None:
            self._jobs_group = None  # back out of one group, to all of them
            self._reload_jobs()
            event.stop()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return

        idx = int(event.row_key.value)

        if self.is_toolbox_active():
            self.run_worker(self._run_toolbox_tool(idx))
            return

        if self.is_top_active():
            self.run_worker(self._jump_from_process_row(idx))
            return

        # only db-mode tabs drill into raw json
        if self._mode != "database":
            return

        if idx >= len(self._dbtab.rows):
            return

        row = self._dbtab.rows[idx]

        # in Jobs, a row is a group of jobs until it is one job: the first
        # enter opens the group, the next opens that job's raw json
        if self.is_jobs_active() and self._jobs_group is None:
            self._jobs_group = (str(row.get("function", "")), str(row.get("state", "")))
            self._reload_jobs()
            return

        self._show_raw(row)

    def _render_actions(self) -> None:
        """Rebuild the action strip under the body from the active tab's own
        actions, and hide it entirely for a tab that has none.

        A dedicated zone rather than a row in the table: these act on the
        database, not on whatever the cursor is on, and a table row reads as
        data (it sorts, it numbers, enter drills into it). More of them are
        expected here — "Create test job" next.
        """
        bar = self.query_one("#acactions", Horizontal)
        bar.display = bool(self._dbtab.actions)

        # every tab load calls this, and a Jobs reload (drilling in, backing
        # out, requeueing) asks for the same buttons it already has: rebuild
        # only on an actual change, so they don't blink under the cursor
        wanted = [action_id for action_id, _label in self._dbtab.actions]
        if self._actions_shown == wanted:
            return

        self._actions_shown = wanted
        bar.remove_children()
        for action_id, label in self._dbtab.actions:
            bar.mount(Button(label, id=action_id, variant="warning", compact=True))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == _REQUEUE_ACTION[0]:
            self.run_worker(self._confirm_requeue())

    async def _confirm_requeue(self) -> None:
        """Put every started/enqueued job back to pending, on confirmation.

        Confirmed like the Toolbox's signals are: it writes to the db, and
        `started` rows belonging to a *live* worker are reset too, so a
        runner that is merely slow has its jobs picked up a second time.
        """
        if self._db is None:
            return

        inst, db = self._db
        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(
                f"Requeue every started/enqueued job on {db} back to pending?\n"
                "Jobs a live worker is still running are reset too, and will run again."
            )
        )
        if not confirmed:
            return

        host = self.app.host
        port = await to_thread(db_port_of, inst, host)
        rows, error = await to_thread(requeue_jobs, db, port, host)

        if error:
            self.app.notify(_first_line(error), severity="warning", timeout=5)
            return

        self.app.notify(f"{db}: {rows} job(s) requeued", timeout=3)
        if self.is_jobs_active():
            self._reload_jobs()

    async def _run_toolbox_tool(self, idx: int) -> None:
        """Run the selected Toolbox row -- signals go to the master pid
        (confirmed first); non-signal entries dispatch to their own
        handler below."""
        if self._instance is None or not (0 <= idx < len(self.TOOLBOX_TOOLS)):
            return

        label, sig = self.TOOLBOX_TOOLS[idx]
        inst = self._instance
        host = self.app.host

        if sig is None:
            await self.copy_shell_command(inst, host)
            return

        if sig == "count_sessions":
            await self._count_sessions(inst, host)
            return

        confirmed = await self.app.push_screen_wait(ConfirmScreen(f"{label} — {inst['name']}?"))
        if not confirmed:
            return

        pid = await to_thread(instance_pid, inst, host)
        if pid is None:
            self.app.notify(f"{inst['name']}: no master pid found", severity="warning", timeout=3)
            return

        await to_thread(signal_process, pid, sig, host)
        self.app.notify(f"{label} sent to {inst['name']} (pid {pid})", timeout=2)

    async def copy_shell_command(self, inst: Instance, host: Host) -> None:
        """Copy `inst`'s `odoo shell` launch command -- ssh-wrapped first if
        `host` isn't local, since the command's own paths (venv, odoo-bin,
        config) only exist on `host`, not necessarily on the machine
        odoo-activity itself is running on. Called from the Toolbox row and
        the app's own `S` shortcut alike."""
        cmd = await to_thread(shell_command, inst, host)
        if cmd is None:
            self.app.notify(f"{inst['name']} isn't running — no shell command to copy", severity="warning", timeout=3)
            return

        text = host.shell_invocation(cmd)
        # local run -> pyperclip (this machine's clipboard); SSH -> OSC 52 (the
        # one that survives the tunnel back to the user's own machine)
        if not await to_thread(try_local_clipboard, text):
            self.app.copy_to_clipboard(text)
        self.app.notify("Shell command copied to clipboard", timeout=3)

    async def _count_sessions(self, inst: Instance, host: Host) -> None:
        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(f"Count sessions for {inst['name']}? (walks the session dir, may be slow)")
        )
        if not confirmed:
            return

        session_dir = await to_thread(session_dir_of, inst, host)
        if session_dir is None:
            self.app.notify(f"{inst['name']}: no data_dir configured", severity="warning", timeout=3)
            return

        count = await to_thread(session_count, session_dir, host)
        self.app.notify(f"{count} sessions in {session_dir}", timeout=5)

    async def _jump_from_process_row(self, idx: int) -> None:
        """`enter` on a postgres row moves the cursor to the Odoo worker
        driving it, traced via the backend's client port (see
        probes.odoo_pid_for_port). A no-op on an Odoo row, or when nothing
        in this table matches (a different instance's worker, a unix-socket
        connection with no port, `lsof` missing, or the worker filtered out
        of the table)."""
        if not (0 <= idx < len(self._proc_rows)):
            return

        row = self._proc_rows[idx]
        if row.get("kind") != "pg":
            return

        port = pg_client_port(row["cmd"])
        target = await to_thread(odoo_pid_for_port, port, self.app.host) if port else None
        row_idx = next((i for i, p in enumerate(self._proc_rows) if p["pid"] == target), None)

        table = self.query_one("#actable", DataTable)
        try:
            position = table.get_row_index(str(row_idx)) if row_idx is not None else None
        except RowDoesNotExist:
            position = None

        if position is None:
            self.app.notify("No matching Odoo process found", severity="warning", timeout=2)
            return

        table.move_cursor(row=position)

    def _show_raw(self, row: dict) -> None:
        self._showing_raw = True
        self._use("raw")
        text = json.dumps(row, indent=2, default=str)
        theme = "ansi_dark" if self.app.current_theme.dark else "ansi_light"
        syntax = Syntax(text, "json", theme=theme, background_color="default", word_wrap=True)
        self.query_one("#acraw-body", Static).update(syntax)

    def on_input_changed(self, event: Input.Changed) -> None:
        # diagnostic-only (see --debug): every value change to the search
        # box, to tell apart real character loss/reordering from a
        # display/repaint lag when a keystroke doesn't seem to show up.
        if event.input.id == "acsearch":
            _log.debug("acsearch value=%r cursor=%r", event.value, event.input.cursor_position)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "acsearch":
            return

        query = event.value.strip() or None
        event.input.display = False
        self.focus_active()

        self._apply_filter(query)
        self.border_title = self._title()  # _title() is otherwise only recomputed on tab switch

    def _apply_filter(self, query: str | None) -> None:
        """Store `query` as the active tab's filter and redraw it from
        what's already loaded -- no probe/odoo-db round trip."""
        tab = self._active_tab()
        if query is None:
            self._filters.pop(tab, None)
        else:
            self._filters[tab] = query

        if self._mode == "database":
            self._show_datatable()
        elif self.is_top_active():
            self._show_process_table()
        elif self.is_stacks_active():
            self._populate_stacks(self._instance)
        else:
            self._render_log()

    def show_instance(self, inst: Instance | None) -> None:
        """Switch to instance mode for `inst`."""
        self._dbtab.abandon()  # leaving database mode; don't leave a fetch running unseen
        self._jobs_group = None  # nothing to back out of once the db is gone
        self._mode = "instance"
        if _inst_key(inst) != _inst_key(self._instance):
            # another instance's processes/threads
            self._filters.pop("Top", None)
            self._filters.pop("Stacks", None)

        self._instance = inst
        self._restore_stacks(inst)
        self._render_mode()

    def show_database(self, inst: Instance, db: str) -> None:
        """Switch to database mode for `db`."""
        self._mode = "database"
        self._db = (inst, db)
        self._restore_stacks(inst)
        self._render_mode()

    def _restore_stacks(self, inst: Instance | None) -> None:
        """Show `inst`'s cached dump if the Stacks tab is actually the one
        visible, else just drop whatever's there — a stale dump from
        whatever was highlighted before must not linger.

        Populating the tree is O(total frames) (every worker/thread/frame
        becomes a node up front) — measured at 80-250ms for a realistic
        multi-worker dump — so doing that unconditionally on every arrow-key
        instance nav, even with Stacks off screen, stalled the event loop
        long enough to delay/drop the next keypress. `_render_active`'s
        Stacks branch calls `_populate_stacks` again once the tab is
        actually the one being switched to.
        """
        if self.is_stacks_active():
            self._populate_stacks(inst)
        else:
            self.query_one("#acstacks", Tree).clear()

    def _populate_stacks(self, inst: Instance | None) -> None:
        # main-thread widget mutation, O(total frames) -- see the module
        # docstring on _restore_stacks; timed here so a --debug run can
        # confirm/rule this out as the source of a felt input stall
        started = time.monotonic()
        tree = self.query_one("#acstacks", Tree)
        cached = self._stacks_cache.get(_inst_key(inst) or "")
        tree.clear()
        if cached is not None:
            workers, workdir = cached
            query = self._filters.get("Stacks")
            if query:
                workers = filter_workers(workers, query)
            if workers:
                render_stacks(tree, workers, workdir)
            else:
                # show_root is False, so an empty tree looks like "no dump yet"
                tree.root.add_leaf(f"(no match: {query})")
        _log.debug("_populate_stacks took %.0fms", (time.monotonic() - started) * 1000)

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

    def focus_active(self) -> None:
        """Move keyboard focus onto whichever body widget the active tab is
        currently showing, so arrow keys scroll/navigate it and `f` maximizes
        it -- called by the app on an explicit tab-switch keybinding, never
        from highlight-driven re-renders (that would fight #instances for
        focus on every arrow-key move)."""
        for selector, widget_type in self._BODY_WIDGETS.values():
            widget = self.query_one(selector, widget_type)
            if widget.display:
                widget.focus()
                return

    def render_stacks(self, inst: Instance, workers: list[Worker], workdir: Path) -> bool:
        """Cache `inst`'s dump (see probes.dump_and_parse_stacks) and, if
        `inst` is still the highlighted instance — the dump takes a couple
        seconds, and the user may have moved on by the time it lands — render
        it into the Stacks tab now. Returns True if anything was busy."""
        self._stacks_cache[_inst_key(inst) or ""] = (workers, workdir)
        if _inst_key(inst) == _inst_key(self._instance):
            self._populate_stacks(inst)  # goes through the search filter, unlike render_stacks
        return any(not t["idle"] for w in workers for t in w["threads"])

    def prev_tab(self) -> None:
        self.select_tab(self._tab - 1)

    def next_tab(self) -> None:
        self.select_tab(self._tab + 1)

    def tick(self) -> None:
        """Keep Top live while it's the active tab. Rides the host
        refresh timer, so it inherits that timer's local/remote cadence —
        CPU% is a rate between two of these samples, so it needs to fire on
        its own rather than only on `R`."""
        if self.is_top_active():
            self._render_top()

    def refresh_active(self) -> None:
        """Manual re-fetch of whatever the active tab shows — the only way
        to update Top on a remote host now that `tick` skips it.

        A refresh stays where the user is: on Jobs that means the group they
        drilled into, which a plain `_render_active` would back out of."""
        self._render_active(keep_group=self.is_jobs_active())

    def _stop_log_stream(self) -> None:
        if self._log_proc is not None:
            self._log_proc.kill()
            self._log_proc = None

    def _append_log(self, data: str) -> None:
        self._log_text += data
        if self._filters.get("Logs"):  # only runs while Logs is active -- see poll()
            self._render_log()
        else:
            body = self.query_one("#acbody", RichLog)
            # only follow the tail if the user was already at the bottom —
            # else a scroll-up to read older lines gets yanked back down
            # every time new data lands
            body.write(data, scroll_end=self._at_bottom(body))

    def poll(self) -> None:
        """Append newly-written log lines while Logs is the active tab. Called
        on a timer; a no-op whenever another tab is active (``_log_path`` is
        None then)."""
        if self._log_proc is not None:
            data = _read_new_bytes(self._log_proc).decode(errors="replace")
            if data:
                self._append_log(data)
            return

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

        self._append_log(data)

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
            title += f" — Config ({self._config_mode})"
            if self._config_path:
                title += f" — {self._config_path}"

        elif self.is_logs_active() and self._log_path:
            title += f" — {self._log_path}"

        elif query := self._active_filter():
            # once the search input hides itself, this border is the only thing
            # left saying the view is filtered rather than short.
            title += f" — {self._active_tab()} (filter: {query})"

        return title

    def _build_tabs(self, names: list[str]) -> None:
        strip = self.query_one("#actabs", Horizontal)
        strip.remove_children()
        strip.mount_all(ActivityTab(name, i, active=(i == self._tab)) for i, name in enumerate(names))

    def _render_active(self, keep_group: bool = False) -> None:
        tabs = self.TABS[self._mode]

        self._tab %= len(tabs)
        for tab in self.query(ActivityTab):
            tab.set_class(tab.index == self._tab, "-active")
        self.border_title = self._title()

        active = tabs[self._tab]
        self._stop_log_stream()
        self._log_path = None
        self._config_path = None
        # never while focused: hiding it drops focus to the ListView mid-typing
        # and the rest is eaten. Reached from more than the tab keys —
        # ListView.clear() emits Highlighted(None), which lands here too.
        search = self.query_one("#acsearch", Input)
        if not search.has_focus:
            search.display = False

        if self._mode == "instance":
            # instance-mode tabs have no db actions; leaving the strip up
            # would offer the last database's ones against nothing
            self._dbtab.actions = []
            self._render_actions()

            if active == "Logs":
                # clear whatever the prior tab left in #acbody (e.g.
                # Top' "Loading top…") instead of leaving it
                # visible until the async tail fetch lands
                self._log_body("Loading logs…")
                self._load_log(self._instance)
            elif active == "Config":
                self._use("log")
                self._render_config()
            elif active == "Processes":
                self._use("processes")
                tree = self.query_one("#acprocesses", Tree)
                tree.clear()
                tree.root.add_leaf("Loading…")
                self._render_processes()
            elif active == "Stacks":
                # deferred here (not on every instance nav — see
                # _restore_stacks) until the tab a user actually switches to.
                # Populating is O(total frames) (80-250ms measured for a
                # realistic multi-worker dump) and mutates the Tree on the
                # main thread (can't to_thread a widget update) -- push it
                # past this switch's own repaint so the keypress that
                # triggered it isn't what stalls.
                self._use("stacks")
                self.call_after_refresh(self._populate_stacks, self._instance)
            elif active == "Toolbox":
                self._render_toolbox()
            else:  # Top
                # loading text now, table (_do_render_top flips _use
                # back once ready) -- a remote fetch is slow enough that an
                # empty, already-visible table read as "nothing happened"
                self._log_body("Loading top…")
                # _top_prev is not cleared here: it is the baseline CPU% is
                # derived from, and _do_render_top rebuilds it from the
                # live pid set anyway. Dropping it made every tab switch show
                # 0.0% until a second refresh -- remotely, a second `R`.
                self._render_top()
        else:
            self._load_db_tab(active, keep_group)

    def _load_log(self, inst: Instance | None) -> None:
        self._coalesce("log", _inst_key(inst), lambda: self._do_load_log(inst))

    async def _do_load_log(self, inst: Instance | None) -> None:
        host = self.app.host
        path = await to_thread(logfile_of, inst, host) if inst else None
        text = await to_thread(tail, path, 200, host) if path is not None else None

        # both awaits above are ssh round trips -- long enough remotely for
        # the tab/instance to have changed since; recheck before spawning a
        # tail -f child or writing into the shared #acbody
        if self._instance is not inst or not self.is_logs_active():
            return

        proc = None
        if path is not None and not host.is_local:
            # host.popen() forks+execs a new ssh child -- to_thread it, same
            # as every other host call here, so that fork never shares the
            # event-loop thread that's also reading keypresses (this is the
            # one spot that used to run it inline
            proc = await to_thread(
                host.popen, ["tail", "-f", "-n", "0", str(path)], stderr=subprocess.DEVNULL, text=False
            )
            if self._instance is not inst or not self.is_logs_active():
                proc.kill()  # superseded while the child was still spawning
                return

        self._follow_log(path, text, host, proc)

    def _follow_log(self, path: Path | None, text: str | None, host: Host, proc: subprocess.Popen | None) -> None:
        self._stop_log_stream()
        self._log_path = path
        self._filters.pop("Logs", None)
        self.border_title = self._title()

        if path is None:
            self._log_pos = 0
            self._log_text = "(no logfile configured)"
            self._render_log()
            return

        self._log_text = text or ""
        self._render_log()

        if host.is_local:
            try:
                self._log_pos = path.stat().st_size
            except OSError:
                self._log_pos = 0
            return

        self._log_proc = proc

    def _render_log(self) -> None:
        body = self.query_one("#acbody", RichLog)
        was_at_bottom = self._at_bottom(body)
        body.clear()

        query = self._filters.get(self._active_tab())  # Logs or Config, whichever is showing
        if not query:
            text = self._log_text
        else:
            needle = query.lower()
            lines = [ln for ln in self._log_text.splitlines() if needle in ln.lower()]
            text = "\n".join(lines) if lines else f"(no match: {query})"

        # Tail logs stick to the bottom only if already there (a fresh,
        # still-empty widget counts as "at bottom" so a first load still
        # opens tailing); the static config always opens at the top
        body.write(text, scroll_end=self.is_logs_active() and was_at_bottom)
        if not self.is_logs_active():
            body.scroll_home(animate=False)

    @staticmethod
    def _at_bottom(body: RichLog) -> bool:
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

        host = self.app.host
        config = await to_thread(configfile_of, inst, host)

        # each await above is an ssh round trip -- long enough remotely for
        # the tab/instance to have changed since; recheck before writing
        # into the shared #acbody, or a stale completion overwrites
        # whatever tab (e.g. Logs) is now showing
        if self._instance is not inst or not self.is_config_active():
            return

        if config is None:
            self._show_config_text("(no config file found)")
            return

        self._config_path = config
        version = await to_thread(instance_version, inst, host)
        text = await to_thread(render_config, config, version, self._config_mode, host)

        if self._instance is not inst or not self.is_config_active():
            return

        self._show_config_text(text)

    def _show_config_text(self, text: str) -> None:
        # not a tailed file, so #log_path stays None — poll() then leaves it alone
        self._log_path = None
        self._filters.pop("Config", None)
        self._log_text = text
        self.border_title = self._title()
        self._render_log()

    def _render_processes(self) -> None:
        self._coalesce("processes", _inst_key(self._instance), self._do_render_processes)

    async def _do_render_processes(self) -> None:
        inst = self._instance
        if inst is None:
            self.query_one("#acprocesses", Tree).clear()
            return

        host = self.app.host
        master, rows = await to_thread(instance_workers, inst, host)
        # one postgres lookup for the whole tree, and only to label a role:
        # an unreachable db just leaves the runner under "HTTP Worker"
        port = await to_thread(db_port_of, inst, host)
        jobrunners = await to_thread(jobrunner_pids, port, host)

        if self._instance is not inst or not self.is_processes_active():
            return  # instance or tab changed while this was fetching; the result is stale

        render_processes(self.query_one("#acprocesses", Tree), rows, master, frozenset(jobrunners))

    def _render_top(self) -> None:
        self._coalesce("top", _inst_key(self._instance), self._do_render_top)

    async def _do_render_top(self) -> None:
        inst = self._instance
        if inst is None:
            self._proc_rows = []
            self._use("table")
            self._show_process_table()
            return

        host = self.app.host
        odoo_procs, pg_procs = await to_thread(instance_procs, inst, host)

        if self._instance is not inst or not self.is_top_active():
            return  # instance or tab changed while this was fetching; the result is stale

        procs: list[tuple[ProcRow, str]] = [(p, "odoo") for p in odoo_procs] + [(p, "pg") for p in pg_procs]
        now = time.monotonic()
        prev, self._top_prev = self._top_prev, {}

        ticks_by_pid = await to_thread(proc_cpu_ticks_many, [p["pid"] for p, _ in procs], host)

        # one more round trip (batched -- see proc_cpu_ticks_many) since the
        # guard above; recheck before forcing #actable visible, or a late
        # completion hijacks whatever tab is now showing
        if self._instance is not inst or not self.is_top_active():
            return

        rows: list[ProcDisplayRow] = []
        for p, kind in procs:
            pid = p["pid"]
            ticks = ticks_by_pid.get(pid)
            cpu = 0.0
            time_str = "-"

            if ticks is not None:
                self._top_prev[pid] = (ticks, now)
                secs = int(ticks / CLK_TCK)  # cumulative CPU time (top's TIME+)
                time_str = f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"

                if pid in prev and (dt := now - prev[pid][1]) > 0:
                    cpu = max(0.0, ticks - prev[pid][0]) / CLK_TCK / dt * 100

            rows.append({**p, "kind": kind, "time": time_str, "cpu": f"{cpu:.1f}"})

        # sort by cpu load desc, in order to quickly spot the ones that matters
        rows.sort(key=lambda p: float(p["cpu"]), reverse=True)

        self._proc_rows = rows
        self._use("table")
        self._show_process_table()

    def _visible_proc_rows(self) -> list[tuple[int, ProcDisplayRow]]:
        """(index into `_proc_rows`, row) for the processes passing the search
        filter. The index becomes the DataTable row key, so `K`/`L`/`enter`
        still act on the right process under a filter (see selected_process)."""
        query = self._filters.get("Top")
        if not query:
            return list(enumerate(self._proc_rows))

        return [
            (i, p) for i, p in enumerate(self._proc_rows) if row_matches({k: p[k] for k in self.TOP_SEARCH_KEYS}, query)
        ]

    def _show_process_table(self) -> None:
        # Same reason as _show_datatable: on the switch-away-from-"Loading…"
        # reveal, the table was hidden until this call, so its post-layout
        # size isn't known yet — sizing COMMAND off it here wraps narrow for
        # one frame, then jumps wide once a tick/resize lands. Deferring
        # sizing until after the reveal's layout pass avoids that flash.
        self.call_after_refresh(self._populate_process_table, self._visible_proc_rows())

    def _populate_process_table(self, pairs: list[tuple[int, ProcDisplayRow]]) -> None:
        """Populate the DataTable, preserving the user's selected PID and
        scroll position across refreshes."""
        table = self.query_one("#actable", DataTable)
        rows = [p for _i, p in pairs]

        keep_pid = table.get_row_at(table.cursor_row)[0] if table.row_count else None
        # clear(columns=True) below wipes both scroll axes too, not just the
        # rows — without saving them here, the 1s tick would yank a
        # manually-scrolled (horizontally or vertically) view back to the
        # top-left corner on every refresh
        scroll_x, scroll_y = table.scroll_x, table.scroll_y

        table.clear(columns=True)
        headers = ("PID", "PPID", "USER", "TIME", "CPU%", "MEM%")
        fields = ("pid", "ppid", "user", "time", "cpu", "mem")
        table.add_columns(*headers)
        # COMMAND takes whatever's left of the table's width (re-derived every
        # tick, so it tracks terminal resizes) instead of a guessed constant
        other_width = sum(
            max(len(h), max((len(p[f]) for p in rows), default=0)) + 2 * table.cell_padding
            for h, f in zip(headers, fields, strict=True)
        )
        table.add_column("COMMAND", width=max(20, (table.size.width or 80) - other_width))
        for i, p in pairs:
            table.add_row(
                p["pid"],
                p["ppid"],
                p["user"],
                p["time"],
                p["cpu"],
                p["mem"],
                Text(p["cmd"], no_wrap=False),
                key=str(i),
                height=None,
            )

        if not rows:
            return

        # Find where the old PID moved to, default to row 0 if missing
        restore = next((i for i, p in enumerate(rows) if p["pid"] == keep_pid), 0)
        table.move_cursor(row=restore)
        table.scroll_to(x=scroll_x, y=scroll_y, animate=False)

    def _reload_jobs(self) -> None:
        """Re-fetch the Jobs tab at its current depth, keeping focus in the
        pane.

        The table is rebuilt from scratch by the reload, and the widget the
        focus was on goes with it — leaving focus outside the pane, where
        `escape` (backing out of a group) never reaches `on_key`.
        """
        self._load_db_tab("Jobs", keep_group=True)
        self.focus_active()

    def _load_db_tab(self, category: str, keep_group: bool = False) -> None:
        self._showing_raw = False
        if not keep_group:
            # a Jobs reload (drilling in, backing out, requeueing) is the same
            # tab with the same actions -- only a real tab change clears them
            self._jobs_group = None
            self._dbtab.actions = []
            self._dbtab.numbered = False
            self._render_actions()

        self._log_body(f"Loading {category.lower()}…")  # clear any prior tab's table while this one loads

        if self._db is None:
            self._log_body("(no database)")
            return

        _inst, db = self._db
        ident = (category, db, self._jobs_group)
        if ident == self._dbtab.ident and self._dbtab.proc is not None:
            return  # already fetching this exact tab; let it finish rather than restart

        if ident != self._dbtab.ident:
            # a different tab/db: its filters mean nothing here, while `R` on
            # the same one keeps them. Read before abandon(), which clears `ident`.
            self._filters.pop(category, None)
            self._show_all = False

        self._dbtab.abandon()  # drop the previous tab's client (see start_odoo_db)
        self._dbtab.ident = ident
        self.run_worker(self._fetch_db_tab(category, db, ident), group="dbtab")

    async def _fetch_db_tab(self, category: str, db: str, ident: tuple[str, str, tuple[str, str] | None]) -> None:
        host = self.app.host
        port = await to_thread(db_port_of, self._db[0], host) if self._db else None

        if category == "Queries":
            # see `long_queries`
            rows = await to_thread(long_queries, db, port, host)
            if ident != self._dbtab.ident:
                return

            self._handle_rows(rows)
            return

        if category == "Jobs":
            await self._fetch_jobs(db, port, ident, host)
            return

        # fetch the flagged-off rows too, so `A` toggles client-side (see _visible_db_rows)
        include_inactive = category.lower() in ALL_ROW_FLAGS
        self._dbtab.no_all = False
        outcome = await self._run_odoo_db(category, db, port, host, ident, include_inactive)
        if outcome is None:
            return

        rows, raw = outcome
        if rows is None and include_inactive and "no such option" in raw.lower():
            # this host's odoo-db needs updating for --all -- retry plain so
            # the tab still gets its rows; toggle_show_all explains the rest.
            self._dbtab.no_all = True
            outcome = await self._run_odoo_db(category, db, port, host, ident, include_inactive=False)
            if outcome is None:
                return

            rows, raw = outcome

        self._handle_rows(rows, raw)

    async def _run_odoo_db(
        self,
        category: str,
        db: str,
        port: str | None,
        host: Host,
        ident: tuple[str, str, tuple[str, str] | None],
        include_inactive: bool,
    ) -> tuple[list[dict] | None, str] | None:
        """One odoo-db run for `category`. None when there's nothing to show
        for it: no odoo-db, timed out, or the tab moved on meanwhile (the
        first two report themselves in the body)."""
        proc = await to_thread(
            start_odoo_db,
            category.lower(),
            db,
            port,
            host,
            include_sensitive_information=self.app.include_sensitive_information,
            include_inactive=include_inactive,
        )
        if proc is None:
            if ident == self._dbtab.ident:
                self._log_body("(odoo-db not found on PATH)")
            return None

        self._dbtab.proc = proc

        def _wait() -> tuple[str, str] | None:
            try:
                return proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:  # backstop for a genuinely stuck call
                proc.kill()
                return None

        result = await to_thread(_wait)

        if ident != self._dbtab.ident:
            return None  # superseded by a newer tab selection; this result is stale

        self._dbtab.proc = None

        if result is None:
            self._log_body("(odoo-db timed out after 90s)")
            return None

        return parse_odoo_db_output(*result)

    async def _fetch_jobs(
        self, db: str, port: str | None, ident: tuple[str, str, tuple[str, str] | None], host: Host
    ) -> None:
        """The Jobs tab, in its two depths: every function/state group, or
        the individual jobs inside the one that's open.

        odoo-db's `jobs` counts by state alone, which says a queue is backed
        up without saying what's stuck in it (see job_groups).
        """
        if self._jobs_group is None:
            rows, error = await to_thread(job_groups, db, port, host)
        else:
            function, state = self._jobs_group
            rows, error = await to_thread(jobs_in_group, db, function, state, port, host)

        if ident != self._dbtab.ident:
            return  # superseded by a newer tab selection; this result is stale

        # offered even with nothing to show: a queue whose jobs are all stuck
        # is exactly when the table above is unhelpful
        self._dbtab.actions = [_REQUEUE_ACTION]
        self._dbtab.numbered = True  # both depths are lists to count: groups, then jobs
        self._handle_rows(rows, _first_line(error))
        self._render_actions()

    def _handle_rows(self, rows: list[dict] | None, raw: str = "") -> None:
        if rows is None:
            self._log_body(raw or "(no output)")
        elif not rows:
            self._dbtab.rows = []  # or a re-render (resize, `A`, a search) repaints the last tab's rows
            self._log_body("(empty)")
        else:
            self._dbtab.rows = rows
            self._show_datatable()

    _BODY_WIDGETS: ClassVar = {
        "log": ("#acbody", RichLog),
        "table": ("#actable", DataTable),
        "raw": ("#acraw", VerticalScroll),
        "stacks": ("#acstacks", Tree),
        "processes": ("#acprocesses", Tree),
    }

    def _use(self, which: str) -> None:
        """Show the Log, the DataTable, the raw-json view, the Stacks tree,
        or the Processes tree in the pane body."""
        for name, (selector, widget_type) in self._BODY_WIDGETS.items():
            self.query_one(selector, widget_type).display = name == which

    def _render_toolbox(self) -> None:
        table = self.query_one("#actable", DataTable)
        table.clear(columns=True)
        self._use("table")
        table.add_column("TOOL")
        for i, (label, _sig) in enumerate(self.TOOLBOX_TOOLS):
            table.add_row(label, key=str(i))

    def _visible_db_rows(self) -> list[tuple[int, dict]]:
        """(original index, row) for the db rows passing the active-flag and
        search filters.

        The index is the row's position in `_dbtab.rows` — it becomes the
        DataTable row key, so drilling into a row's raw json still lands on
        the right row under a filter (see on_data_table_row_selected)."""
        flag = self._all_row_flag()
        query = self._filters.get(self._active_tab())

        pairs = []
        for i, row in enumerate(self._dbtab.rows):
            if flag is not None and not self._show_all and not row.get(flag, True):
                continue
            if query and not row_matches(row, query):
                continue

            pairs.append((i, row))

        return pairs

    def _show_datatable(self) -> None:
        if not self._dbtab.rows:
            return  # nothing loaded yet (error/(empty)) -- don't clobber that message

        self._showing_raw = False
        pairs = self._visible_db_rows()
        if not pairs:
            query = self._filters.get(self._active_tab())
            self._log_body(f"(no match: {query})" if query else "(empty)")
            return

        # Unlike Top (re-rendered every 0.5s poll, so a wrong width
        # self-corrects within a tick), db tabs render once — reveal the
        # table first so layout can size it, then measure it, instead of
        # sizing the wrap column off its pre-layout (e.g. leftover/zero)
        # width and being stuck that way until an actual terminal resize.
        self.query_one("#actable", DataTable).clear(columns=True)  # drop stale rows before the reveal
        self._use("table")
        self.call_after_refresh(self._populate_datatable, pairs)

    def _populate_datatable(self, pairs: list[tuple[int, dict]]) -> None:
        table = self.query_one("#actable", DataTable)
        table.clear(columns=True)
        rows = [row for _i, row in pairs]
        columns = table_columns(rows)

        # "code" (crons' --include-code) and "value" (params) are blobs, not
        # cells — wrap them into the table's remaining width instead of
        # stringify's 80-char clip, same as the Top tab's COMMAND column.
        wrap_col = next((c for c in ("code", "value") if c in columns), None)
        fixed = [c for c in columns if c != wrap_col]
        if self._dbtab.numbered:
            table.add_column("#")
        table.add_columns(*(c.upper() for c in fixed))
        if wrap_col:
            other_width = sum(
                max(len(c), max((len(stringify(row.get(c, ""))) for row in rows), default=0)) + 2 * table.cell_padding
                for c in fixed
            )
            table.add_column(wrap_col.upper(), width=max(20, (table.size.width or 80) - other_width))

        for n, (i, row) in enumerate(pairs, start=1):
            cells: list[str | Text] = [stringify(row.get(c, "")) for c in fixed]
            if wrap_col:
                cells.append(Text(str(row.get(wrap_col, "")), no_wrap=False))
            if self._dbtab.numbered:
                # counts what's on screen, so a filtered list still reads 1..n
                cells.insert(0, Text(str(n), style="dim"))
            table.add_row(*cells, key=str(i), height=None)

    def _log_body(self, text: str) -> None:
        self._use("log")
        body = self.query_one("#acbody", RichLog)
        body.clear()
        body.write(text)
