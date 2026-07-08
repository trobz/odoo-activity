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
for the database category tabs. The Config tab additionally needs
`odoo-config` and `odoo-addons-path` on `PATH`.

| Key | Action |
| --- | --- |
| `↑`/`↓` | move through instances and their nested dbs |
| `s` / `r` | start/stop toggle / restart (confirm popup) |
| `[` / `]` | switch tab in the detail pane |
| `p` / `l` / `c` | Processes / Logs / Config |
| `u` / `l` / `j` / `c` | Users / Locks / Jobs / Crons |
| `K` | kill -9 the selected process (Processes tab, confirm popup) |
| `L` | kill -3 the selected process, then jump to Logs (Processes tab) |
| `e` | cycle compact/explain/expand/clean (Config tab) |
| `/` | search (Logs and Config tabs) |
| `q` | quit |

### Config tab modes

`e` cycles the Config tab through `odoo-config`'s `compact`/`explain`/
`expand`/`clean` views of the highlighted instance's config file — see
[odoo-config's CLI docs][odoo-config-cli] for what each one shows.

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
- **`panes/detail.py`** — `ActivityPane`, the one stateful render widget: a
  tab strip over a Log/DataTable, mode-switched by whatever's highlighted
  (see Modes below) — not a separate popup screen.
- **`tui.py`** — the shell only: `compose()` layout, the nested instances+dbs
  `ListView`, focus/highlight wiring, refresh timers, start/stop/restart +
  `ConfirmScreen`. Delegates rendering to `ActivityPane`, data to `probes.py`.

### Modes

`ActivityPane` mode-switches on whatever's highlighted in the instances list:

- **Instance mode** — an instance row is highlighted. Tabs: Processes, Logs,
  Config.
- **Database mode** — one of its nested database rows is highlighted. Tabs:
  Users, Locks, Jobs, Crons, Modules, Stats.

Both modes share the same tab strip and Log/DataTable widgets (just a
`_mode` flag), and several letter-key shortcuts are reused across them for
whichever tab they map to in each (e.g. `l` is Logs in instance mode, Locks
in database mode).

### Data sources

- **Instances** — `systemctl --user list-units` and `supervisorctl status`,
  merged by name.
- **Databases** — each instance's `<workdir>/config/{odoo.conf,server.conf}`
  gives a db role (or `ODOO_ACTIVITY_DB_ROLE`); `psql` lists the databases owned
  by that role.
- **Processes** — the manager gives the instance's master pid (`systemctl ...
  -p MainPID` / `supervisorctl pid`); `ps -eo pid,ppid,user,%mem,args` is then
  walked down the ppid tree from there to find every worker.
- **Logs** — the same config gives `logfile`, tailed by reading backward in
  fixed-size chunks from the end so a multi-GB file costs a few reads, not a
  full scan.
- **Config** — read-only: `odoo-config {compact,explain,expand,clean}` is run
  against the instance's config file and its plain-text stdout is shown as-is;
  the version passed to it comes from `odoo-addons-path <workdir> --verbose
  --format json`'s `version` key.

[odoo-config-cli]: https://github.com/trobz/odoo-config/blob/main/CLI.md
