---
icon: lucide/plug
description: Expose the same read-only data as an MCP server, for an agent to work an investigation alongside a human on the TUI.
tags:
  - mcp
  - agent
---

# MCP Server

`oa-mcp [host]` exposes the same read-only data as an MCP server, for an
agent to work an investigation alongside a human on `oa [host]` — both
looking at the same target. Every tool call is pinned to `host` (local if
omitted); a `host`/`ssh_port` argument on a tool call must match the pin
or is rejected.

`oa-mcp-multi` instead leaves the target per-call, capped by
`--host-filter` (an odoo dbfilter-style regex; unset means unrestricted)
and `--host-file` (which `~/.ssh/config`-style file reads aliases from).

Both default to the `stdio` transport (spawned by the MCP client); add
`--transport streamable-http --bind-host ... --bind-port ...` to run as a
network server instead.

## Sensitive data

`db_query`'s `params` output is masked by default, unlike the TUI's: a tool
call has no human at the screen, and the plaintext would land in the
agent's context. Unmasking is launch-time only, via
`--include-sensitive-information` on the `oa-mcp`/`oa-mcp-multi` command
line — never a per-call tool argument, so no tool call can turn it on
itself. `mail_audit` (outbound mail config) follows the same rule for
`smtp_user`/`smtp_pass`.

## `--enable-plugins=odooly`

The one non-read-only exception: `list_odooly_envs`, `instance_odooly_env`,
and `odooly_run_script` match a database against `~/odooly.ini` and run the
packaged scripts (`create_test_job`, `restore_app_icons`, `send_test_mail`),
the same actions the TUI's odooly plugin offers a human through the
Toolbox — now callable by the agent directly. Odooly logs in and can write
to the matched database, so following the same launch-time-only pattern as
`--include-sensitive-information`, `--enable-plugins` (comma-separated, or
repeat the flag) can only be set on the command line when the server
starts — no tool call can enable it from within a session, and all three
tools raise if `odooly` wasn't named there. It needs the `odooly` extra
installed too. Unlike the TUI, the flag stays: exposing write actions to an
agent is worth deciding explicitly, and installing a package is not that
decision.

`odooly_run_script`/`instance_odooly_env` always resolve locally against
this machine's own `~/odooly.ini`, regardless of `host`/`ssh_port` used to
pin the server — odooly reaches instances over the network, not over ssh.

`send_test_mail` sends one real email through `mail.mail` to check outbound
mail actually reaches an inbox; it requires a `to` recipient, validated
before the script is even invoked.

See [Keybindings & Tabs — Odooly](keybindings.md#odooly) for
what each script does.
