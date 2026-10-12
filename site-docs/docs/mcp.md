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

`db_query`'s `params` and `check-sensitive-information` output is masked by
default, unlike the TUI's: a tool
call has no human at the screen, and the plaintext would land in the
agent's context. Unmasking is launch-time only, via
`--include-sensitive-information` on the `oa-mcp`/`oa-mcp-multi` command
line — never a per-call tool argument, so no tool call can turn it on
itself. `mail_audit` (outbound mail config) follows the same rule for
`smtp_user`/`smtp_pass`.

## `--enable-plugins=odooly`

The one non-read-only exception: `list_odooly_envs`, `instance_odooly_env`,
and the packaged scripts as their own tools — `create_test_job`,
`restore_app_icons`, `send_test_mail` — match a database against
`~/odooly.ini` and run them, the same actions the TUI's odooly plugin offers
a human through the Toolbox — now callable by the agent directly, one tool
per script rather than a single dispatcher, so each shows up by name in an
agent's tool list. Odooly logs in and can write to the matched database, so
following the same launch-time-only pattern as
`--include-sensitive-information`, `--enable-plugins` (comma-separated, or
repeat the flag) can only be set on the command line when the server
starts — no tool call can enable it from within a session, and all three
script tools raise if `odooly` wasn't named there. It needs the `odooly`
extra installed too. Unlike the TUI, the flag stays: exposing write actions
to an agent is worth deciding explicitly, and installing a package is not
that decision.

`create_test_job`/`restore_app_icons`/`send_test_mail`/`instance_odooly_env`
always resolve locally against this machine's own `~/odooly.ini`, regardless
of `host`/`ssh_port` used to pin the server — odooly reaches instances over
the network, not over ssh.

`send_test_mail` sends one real email through `mail.mail` to check outbound
mail actually reaches an inbox; it requires a `to` recipient, validated
before the script is even invoked.

See [Keybindings & Tabs — Odooly](keybindings.md#odooly) for
what each script does.

## `--enable-plugins=pos`

Adds `pos_status(env)`: the same point-of-sale status the TUI's opt-in POS
tab shows — each `pos.config`'s session status/order count, latest session
open time and latest order date, payment methods and whether they wait for
the terminal's own confirmation, and IoT Box/proxy/device settings. Unlike
odooly's tools it's read-only, but it still needs a login, so it's gated
the same launch-time-only way: naming `pos` pulls in `odooly` too (matching
`Plugin.requires`, the same as `oa --enable-plugins=pos` does), and it
needs the `pos` extra installed (`odoo-activity[pos]`).

Resolves locally against this machine's own `~/odooly.ini`, same as
`create_test_job`/`restore_app_icons`/`send_test_mail` — reaches the
instance over the network, not over ssh, regardless of `host`/`ssh_port`
used to pin the server.

See [Keybindings & Tabs — POS](keybindings.md#pos) for what each field
means, including how it degrades on an instance whose version is missing a
field.

## `--enable-plugins=password-audit`

This is a credential-material and local-GPU exception to the read-only
surface. Both `oa-mcp` and `oa-mcp-multi` require the operator to enable
`password-audit` at launch; installing the plugin does not enable it, and
no per-call argument can bypass the gate. Export probes follow the pinned
host or multi-host filter. Planning, calibration, attacks, status and
findings run on the **MCP server machine**, not the database host.

| Tool | Contract |
| --- | --- |
| `password_hashes` | Export active users' convertible hashes into a fresh private directory; return exact hash, derived/tier-2 wordlist and login-map paths, not hashes. |
| `password_plan` | Read exports and both companion wordlists, calibrate synthetic wordlist attacks at each actual round count, and fund passes across databases under one explicit total-minute budget. Missing companions are errors, never fabricated candidate lists. |
| `password_crack` | With a wordlist, launch a detached bounded attack; without one, only show prior audit-local cracks. Always supply explicit `minutes` when executing a plan. |
| `password_crack_status` | Poll durable running/completed/error state after launch or a disconnected call; do not launch a replacement while the pass is running. |
| `password_findings` | Return structured findings joined to the export and audit-local potfile for report aggregation; this can contain plaintext passwords. |

### Budgeted workflow

1. Export each authorized database and retain the exact returned paths.
2. Call `password_plan(audits=[{"hash_file": "<exact export path>"}], minutes=...)`.
   Calibration and input-reading time are charged to the total budget.
   Planning is capped at the smaller of that budget and 250 seconds; failure
   or deadline exhaustion returns an error, never a partial plan. Calibration
   uses `min(8, smallest actual salt-group count)` for each round count in
   the audit. The output names both the synthetic benchmark shape and the
   smallest actual count. Low-salt audits are not assigned an optimistic
   eight-salt rate; for larger counts, the measured eight-salt rate is used
   conservatively, without fabricated throughput scaling.
3. Follow funded passes in order. Each pass names its exact hash and wordlist
   paths and explicit minute/runtime cap; unfunded/skipped passes are not
   launch instructions. **All wordlist passes are asynchronous**, even short
   ones: poll `password_crack_status` until completion before another pass.
4. Preserve the original audit deadline. Before *every* pass, cap its seconds
   to the smaller of its listed cap and the floored actual remaining audit
   time, then divide by 60 for `minutes` (round down if floating-point
   `ceil(minutes*60)` would exceed the cap). If less than one second remains,
   stop. Startup, polling and operator delays consume budget too. Never omit
   `minutes`: the independent 250-second crack default does not implement the
   plan. Generic wordlists are optional only within actual remaining time.
5. Collect `password_findings`, produce the authorized report, and apply the
   agreed retention/deletion policy to the entire audit directory.

Planner inputs must be regular files (not FIFOs/devices). Limits are 128
exports, 32 MiB per hash/wordlist file, 200,000 records per file and 16 KiB
per record; excess returns an explicit error. Streaming reads check the
shared planning deadline between records.

### Retention and checkpoints

Treat exports, login mappings, wordlists, the canonical audit-local potfile,
plaintext findings, logs, private input snapshots and checkpoints as
credential material. Keep them private and off shared disks; there is no
silent dependency on the global hashcat potfile. Agree retention before
exporting and delete the **whole export directory** once the audit and any
approved resumption are finished, only after its detached worker completes.

Rerunning the same attack resumes only matching hash/wordlist/rules content
fingerprints and private checkpoints; changed inputs must not silently
resume old work. The worker holds the attack lock and publishes a durable
completion code. Modern hashcat restore-position support and validated
legacy checkpoints must honor the new bounded runtime rather than restoring
an old unlimited/stale budget. A server disconnect does not cancel the
worker: inspect status first, never truncate artifacts or start overlapping
passes while its outcome is unknown.
