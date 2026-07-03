# odoo-activity

A terminal UI for local Odoo instances. One screen: host cpu/mem/uptime, every
Odoo instance (`systemd --user` or `supervisor`) with its databases nested
underneath, and a detail pane for process/log/db inspection.

## Installation

```bash
uv tool install odoo-activity
```

## Usage

```bash
odoo-activity  # or: oa
```

Assumes Odoo instances run under `systemd --user` and/or `supervisor`
(both are discovered and merged), and that the `odoo-db` CLI is on `PATH`
for the database category tabs.

| Key | Action |
| --- | --- |
| `↑`/`↓` | move through instances and their nested dbs |
| `s` / `p` / `r` | start / stop / restart (confirm popup) |
| `[` / `]` | switch tab in the detail pane |
| `u` / `l` / `j` / `c` | Users / Locks / Jobs / Crons (db row highlighted) |

`ODOO_ACTIVITY_DB_ROLE` overrides the postgres role used to resolve an
instance's databases (default: the instance's `db_user`, falling back to
its name).

## Architecture

```
odoo_activity/
├── probes.py         # all system data: no Textual import
├── panes/detail.py   # ActivityPane: the one stateful rendering widget
└── tui.py            # app shell: layout, list, timers, actions
```

- **`probes.py`** — pure functions, no UI. Every `systemctl`/`supervisorctl`/
  `ps`/`psql` call and `/proc` read lives here, returning plain dicts/lists
  so it's testable without spinning up a screen. An instance's databases,
  logfile and processes all resolve from **one config**: its
  `<workdir>/config/{odoo.conf,server.conf}`.
- **`panes/detail.py`** — `ActivityPane` mode-switches on what's highlighted:
  `instance` mode shows Top/Logs, `database` mode shows Users/Locks/etc…. Same
  tab strip and Log/DataTable widgets for both — a `_mode` flag, not a separate
  popup screen.
- **`tui.py`** — the shell only: `compose()` layout, the nested instances+dbs
  `ListView`, focus/highlight wiring, refresh timers, start/stop/restart +
  `ConfirmScreen`. Delegates rendering to `ActivityPane`, data to `probes.py`.

### Data sources

- **Instances** — `systemctl --user list-units` and `supervisorctl status`,
  merged by name.
- **Databases** — each instance's `<workdir>/config/{odoo.conf,server.conf}`
  gives a db role (or `ODOO_ACTIVITY_DB_ROLE`); `psql` lists the databases owned
  by that role.
- **Top** — the manager gives the instance's master pid (`systemctl ... -p
  MainPID` / `supervisorctl pid`); `ps -eo pid,ppid,user,%mem,args` is then
  walked down the ppid tree from there to find every worker.
- **Logs** — the same config gives `logfile`, tailed by reading backward in
  fixed-size chunks from the end so a multi-GB file costs a few reads, not a
  full scan.
