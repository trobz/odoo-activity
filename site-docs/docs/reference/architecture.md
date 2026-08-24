---
icon: lucide/layers
description: Module layout, and how instance/database discovery resolves.
tags:
  - architecture
---

# Architecture

```
odoo_activity/
├── host.py            # local vs ssh command dispatch
├── probes.py          # all system data: no Textual import, shared by the TUI and MCP server
├── mcp_server.py      # oa-mcp / oa-mcp-multi: probes.py as a read-only MCP tool API
├── panes/detail.py    # ActivityPane: the one stateful rendering widget
├── panes/processes.py # Processes tab: workers grouped by role
├── panes/stacks.py    # Stacks tab: parsed dumpstacks, busy-first
├── panes/mail.py      # Mail tab: one Rich table per section, into the log body
├── scripts/           # odooly actions the Toolbox shells out to (network, not host)
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
  human (see [MCP Server](../mcp.md)).
- **`panes/detail.py`** — `ActivityPane`, the one stateful render widget: a
  tab strip over a Log/DataTable/Tree, mode-switched by whatever's
  highlighted. Delegates the Processes, Stacks, and Mail tab bodies to
  `panes/processes.py`/`panes/stacks.py`/`panes/mail.py`.
- **`tui.py`** — the shell only: `compose()` layout, the nested
  instances+dbs `ListView`, focus/highlight wiring, refresh timers,
  start/stop/restart. Delegates rendering to `ActivityPane`, data to
  `probes.py`, confirm popups to `panes/confirm.py`'s `ConfirmScreen`
  (shared with `ActivityPane`, which also confirms mutating actions like
  Toolbox).

## Managers

An instance's `manager` — `systemd`, `supervisor`, or `odoosh` — is
discovered per instance, not configured, and decides which controller
process/log/start-stop-restart lookups route through:

- **`systemd`** — a `systemd --user` unit, controlled via `systemctl --user`.
- **`supervisor`** — a `supervisorctl status` program, controlled via
  `supervisorctl`.
- **`odoosh`** — the odoo.sh build a host is running, when odoo-activity
  itself runs directly on that host. One host is one build, so there's
  nothing to enumerate — the whole box is "the instance". Start/stop isn't
  supported (odoo.sh handles sleep/wake on its own); restart goes through
  `odoosh-restart`, needed on `PATH` — which ships pre-installed on
  odoo.sh hosts.

### Config tab modes

`e` cycles the Config tab through `odoo-config`'s `compact`/`explain`/
`expand`/`clean` views of the highlighted instance's config file — see
[odoo-config's CLI docs][odoo-config-cli] for what each one shows.

`ODOO_ACTIVITY_DB_ROLE` overrides the postgres role used to resolve an
instance's databases (default: the instance's `db_user`, falling back to
its name).

## Data sources

- **Instances** — `systemctl --user list-units` and `supervisorctl status`,
  merged by name.
- **Databases** — each instance's `<workdir>/config/{odoo.conf,server.conf}`
  gives a db role (or `ODOO_ACTIVITY_DB_ROLE`); `psql` lists the databases
  owned by that role.
- **Top** — the manager gives the instance's master pid (`systemctl ...
  -p MainPID` / `supervisorctl pid`); `ps -eo pid,ppid,user,%mem,args` is
  then walked down the ppid tree from there to find every worker.
- **Logs** — the same config gives `logfile`, tailed by reading backward in
  fixed-size chunks from the end so a multi-GB file costs a few reads, not
  a full scan.
- **Config** — read-only: `odoo-config {compact,explain,expand,clean}` is
  run against the instance's config file and its plain-text stdout is
  shown as-is; the version passed to it comes from `odoo-addons-path
  <workdir> --verbose --format json`'s `version` key.
- **Params** — `odoo-db params <db>` reads `ir_config_parameter`; `/`
  filters rows by key or value. odoo-db masks secret-looking ones
  (`password`, `token`, an `enterprise_code`, ...) as `********` by
  default, so the TUI always runs it with
  `--include-sensitive-information`.
- **Mail** — `odoo-db mail <db>` audits outbound mail config. Unlike every
  other db tab, it doesn't go through the generic table renderer: the
  sections don't share columns, so `panes/mail.py` renders each non-empty
  one as its own table in the log body instead.

[odoo-config-cli]: https://github.com/trobz/odoo-config/blob/main/CLI.md
