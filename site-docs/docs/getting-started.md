---
icon: lucide/rocket
description: The odoo ops toolbox, for you (TUI) and your agent (MCP)
tags:
  - installation
  - quickstart
---

# Getting Started

A terminal UI for Odoo instances, on this machine or on a remote host over
ssh. One screen: host cpu/mem/uptime, every Odoo instance (`systemd --user`
or `supervisor`) with its databases nested underneath, and a detail pane for
process/log/db inspection.

## Installation

```bash
uv tool install odoo-activity
odoo-activity --version
```

Or with pip:

```bash
pip install odoo-activity
```

## Quick example

```bash
# Watch this machine
oa

# Watch a remote host over ssh
oa odoo@somehost

# ...on a non-default ssh port
oa odoo@somehost -p 10113
```

Discovers Odoo instances under `systemd --user`, `supervisor`, and odoo.sh
(all three are merged), and needs the [`odoo-db`][odoo-db] CLI on `PATH`
for the database category tabs. The Config tab additionally needs
[`odoo-config`][odoo-config] and [`odoo-addons-path`][odoo-addons-path] on
`PATH`.

The Params tab shows `ir_config_parameter` secret-looking values unmasked
by default — you already have a shell on this host. Pass
`--no-include-sensitive-information` to keep odoo-db's own masking instead.

Continue to [Keybindings & Tabs](keybindings.md) for the full reference.

## See also

- [MCP Server](mcp.md) — expose the same data to an agent, alongside a
  human on the TUI.
- [Architecture](reference/architecture.md) — module layout, managers,
  and where each tab's data comes from.

[odoo-db]: https://github.com/trobz/odoo-db
[odoo-config]: https://github.com/trobz/odoo-config
[odoo-addons-path]: https://github.com/trobz/odoo-addons-path
