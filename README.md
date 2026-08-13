# odoo-activity

A terminal UI for Odoo instances, on this machine or on a remote host over
ssh. One screen: host cpu/mem/uptime, every Odoo instance (`systemd --user`
or `supervisor`) with its databases nested underneath, and a detail pane for
process/log/db inspection.

## Installation

```bash
uv tool install odoo-activity
```

## Usage

```bash
odoo-activity                    # or: oa — this machine
oa openerp@somehost              # a remote host over ssh
oa openerp@somehost -p 10113     # ...on a non-default ssh port
```

Discovers Odoo instances under `systemd --user`, `supervisor`, and odoo.sh
(all three are merged), and needs the `odoo-db` CLI on `PATH` for the
database category tabs. The Config tab additionally needs `odoo-config`
and `odoo-addons-path` on `PATH`. See [Managers](#managers) for what each
one supports.

The Params tab shows `ir_config_parameter` secret-looking values unmasked
by default — you already have a shell on this host. Pass
`--no-include-sensitive-information` to keep odoo-db's own masking instead.

| Key | Action |
| --- | --- |
| `↑`/`↓` | move through instances and their nested dbs |
| `s` / `r` | start/stop toggle / restart (confirm popup) |
| `[` / `]` | switch tab in the detail pane |
| `f` | maximize/minimize the focused pane |
| `p` / `l` / `c` / `t` | Top / Logs / Config / Toolbox |
| `u` / `l` / `j` / `c` / `p` | Users / Locks / Jobs / Crons / Params |
| `K` | kill -9 the selected process (Top and Processes tabs, confirm popup) |
| `L` | kill -3 the selected process, then jump to Stacks (Top tab) |
| `D` | dump stacks of all workers, then jump to Stacks |
| `S` | copy the instance's `odoo shell` launch command to the clipboard |
| `e` | cycle compact/explain/expand/clean (Config tab) |
| `A` | show all rows, inactive ones included |
| enter | run the selected tool (Toolbox tab, confirm popup) / open a Jobs group / open a row's raw json (db tabs) |
| escape | back out of a Jobs group, or of a row's raw json |
| `/` | search |
| `R` | refresh the active tab now |
| `q` | quit |

Two tabs on each side have no letter shortcut — cycle to them with
`[`/`]` or click: **Processes** and **Stacks** (instance mode), **Queries**
and **Modules** (database mode).

`A` asks `odoo-db` for the rows it filters out by default (its `--all`
flag). Against a host whose `odoo-db` predates that flag, the tab falls
back to the default rows and `A` says so instead of doing nothing.

### Jobs (`j`)

queue_job's jobs grouped by function and state, numbered, with the oldest
creation date and the longest wait/run in each group — which is what a job
stuck in `started` for hours looks like. Enter opens a group as its
individual jobs (numbered too, `date_created`/`date_started` each, oldest
first, capped at 500), escape backs out, and enter on one of those opens its
raw json.

Under the table is the tab's action strip — buttons that act on the
database rather than on the row under the cursor, so they are not rows
themselves. Jobs has one: **Requeue jobs** puts every `started`/`enqueued`
job back to `pending` (after a confirm popup — including jobs a live worker
is still running, which will then run again), clearing the dates that go
with those states the way queue_job's own `set_pending` does — what a runner
does for its own dead jobs at startup, for when a worker was killed mid-job
and nothing else will revisit the row. It's offered even when the table above is empty, and the strip is
hidden entirely on a tab that has no actions.

The Processes tab lists the queue_job runner as its own role. Odoo only
labels a worker in `ps` when `setproctitle` is installed — with it, that
label is the whole answer and costs nothing. Without it, the runner is found
by its postgres connection instead: `application_name` names the pid outright
from Odoo 16.0 on, and before that (where odoo never set it) the connection
is traced by its TCP endpoint, `ss` or `lsof` saying which process holds the
client port. An instance on a unix socket reports no port and can't be traced
that way; if nothing can account for it, the runner just stays under HTTP
Worker.

Toolbox (`t`) offers four tools:
- Spin a worker up (`SIGTTIN`) or down (`SIGTTOU`).
- Open shell — which copies the launch command instead of signaling, so it needs
  no confirm.
- Count sessions under the instance's data dir (walks the filesystem, may be
  slow).

### Remote hosts

The target is any ssh destination — `[user@]host` or a `~/.ssh/config`
alias. Only the tools already required locally are needed, but on the
remote host. Connections are multiplexed, so the first call opens the
session and the rest reuse it.

Everything still refreshes on its own against a remote host, just on a
slower tick — host stats and Top every 5s, the instance list every
15s. `R` refreshes the active tab immediately, plus the instance list and
the highlighted instance's databases.

## MCP server

`oa-mcp [host]` exposes the same read-only data as an MCP server, for an
agent to work an investigation alongside a human on `oa [host]` — both
looking at the same target. Every tool call is pinned to `host` (local if
omitted); a `host`/`ssh_port` argument on a tool call must match the pin
or is rejected.

`db_query`'s `params` output is masked by default, unlike the TUI's: a tool
call has no human at the screen, and the plaintext would land in the agent's
context. Unmasking is launch-time only, via `--include-sensitive-information`
on the `oa-mcp`/`oa-mcp-multi` command line — never a per-call tool
argument, so no tool call can turn it on itself.

`oa-mcp-multi` instead leaves the target per-call, capped by
`--host-filter` (an odoo dbfilter-style regex; unset means unrestricted)
and `--host-file` (which `~/.ssh/config`-style file reads aliases from).

Both default to the `stdio` transport (spawned by the MCP client); add
`--transport streamable-http --bind-host ... --bind-port ...` to run as a
network server instead.

## Managers

An instance's `manager` — `systemd`, `supervisor`, or `odoosh` — is
discovered per instance, not configured, and decides which controller
process/log/start-stop-restart lookups route through:

- **`systemd`** — a `systemd --user` unit, controlled via `systemctl --user`.
- **`supervisor`** — a `supervisorctl status` program, controlled via
  `supervisorctl`.
- **`odoosh`** — the odoo.sh build a host is running, when odoo-activity
  itself runs directly on that host (installed via `requirements.txt` at
  build time, same as `odoo-config`/`odoo-db`). One host is one build, so
  there's nothing to enumerate — the whole box is "the instance". Start/stop
  isn't supported (odoo.sh handles sleep/wake on its own); restart goes
  through `odoosh-restart`, needed on `PATH` — which ships pre-installed on
  odoo.sh hosts.

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
├── host.py            # local vs ssh command dispatch
├── probes.py          # all system data: no Textual import, shared by the TUI and MCP server
├── mcp_server.py      # oa-mcp / oa-mcp-multi: probes.py as a read-only MCP tool API
├── panes/detail.py    # ActivityPane: the one stateful rendering widget
├── panes/processes.py   # Processes tab: workers grouped by role
├── panes/stacks.py    # Stacks tab: parsed dumpstacks, busy-first
└── tui.py             # app shell: layout, list, timers, actions
```

- **`host.py`** — a `Host` is this machine or an ssh destination. Every probe
  takes one and runs the same way against either, so nothing above this
  layer knows whether it is local or remote.
- **`probes.py`** — pure functions, no UI. Every `systemctl`/`supervisorctl`/
  `ps`/`psql` call and `/proc` read lives here, returning plain dicts/lists
  so it's testable without spinning up a screen. An instance's databases,
  logfile and top all resolve from **one config**: its
  `<workdir>/config/{odoo.conf,server.conf}`.
- **`mcp_server.py`** — thin `@mcp.tool()` wrappers over `probes.py`, no
  logic of its own; the same data the TUI shows, for an agent instead of a
  human (see [MCP server](#mcp-server)).
- **`panes/detail.py`** — `ActivityPane`, the one stateful render widget: a
  tab strip over a Log/DataTable/Tree, mode-switched by whatever's
  highlighted (see Modes below) — not a separate popup screen. Delegates
  the Processes and Stacks tab bodies to `panes/processes.py`/`panes/stacks.py`.
- **`tui.py`** — the shell only: `compose()` layout, the nested instances+dbs
  `ListView`, focus/highlight wiring, refresh timers, start/stop/restart.
  Delegates rendering to `ActivityPane`, data to `probes.py`,
  confirm popups to `panes/confirm.py`'s `ConfirmScreen` (shared with
  `ActivityPane`, which also confirms mutating actions like Toolbox).

### Modes

`ActivityPane` mode-switches on whatever's highlighted in the instances list:

- **Instance mode** — an instance row is highlighted. Tabs: Top,
  Processes, Stacks, Logs, Config, Toolbox.
- **Database mode** — one of its nested database rows is highlighted. Tabs:
  Queries, Users, Locks, Jobs, Crons, Modules, Params.

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
- **Top** — the manager gives the instance's master pid (`systemctl ...
  -p MainPID` / `supervisorctl pid`); `ps -eo pid,ppid,user,%mem,args` is then
  walked down the ppid tree from there to find every worker.
- **Logs** — the same config gives `logfile`, tailed by reading backward in
  fixed-size chunks from the end so a multi-GB file costs a few reads, not a
  full scan.
- **Config** — read-only: `odoo-config {compact,explain,expand,clean}` is run
  against the instance's config file and its plain-text stdout is shown as-is;
  the version passed to it comes from `odoo-addons-path <workdir> --verbose
  --format json`'s `version` key.
- **Params** — `odoo-db params <db>` reads `ir_config_parameter`; `/` filters
  rows by key or value. Values are shown as they are: odoo-db masks
  secret-looking ones (`password`, `token`, an `enterprise_code`, ...) as
  `********` by default, so the TUI always runs it with
  `--include-sensitive-information`.

[odoo-config-cli]: https://github.com/trobz/odoo-config/blob/main/CLI.md
