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

## `--enable-plugins=config-audit`

Bundled and opt-in, with no additional dependencies. Start `oa-mcp` or
`oa-mcp-multi` with `--enable-plugins=config-audit`.

| Tool | What it returns |
|---|---|
| `config_audit` | Installed modules (name, version, auto-install flag) and allowlisted configuration parameters for one database. |

Call with `name` (instance name), `db`, and optionally `host` / `ssh_port`.
The tool uses the instance's PostgreSQL connection and the normal pinned-host
or multi-host restrictions. It returns structured data directly, without files.

Parameters: `auth_signup.invitation_scope`, `auth_password_policy.minlength`,
`auth_totp.policy`, `web.base.url`, `web.base.url.freeze`, `report.url`, and
`database.is_neutralized`. These cover signup/password/MFA policy, generated
links, report rendering, and the neutralization marker. Only those keys are
queried; arbitrary parameters and secrets are excluded.

Values are stored strings, with `null` for an absent key and `""` for a stored
blank. Missing keys do not establish effective defaults: behavior depends on
the Odoo version and installed addons. The neutralization marker alone is not
proof that a database contains no secrets.

## `--enable-plugins=users-audit`

Opt-in batch export of Odoo user accounts and password hashes. Both
`oa-mcp` and `oa-mcp-multi` require the operator to enable `users-audit` at
launch; installing the plugin does not enable it, and no per-call argument
can bypass the gate. Targets are explicit `{host, instance, db}` entries — on
a pinned server every target must be on the pinned host, on `oa-mcp-multi`
each host is checked against `--host-filter`. Targets run sequentially and a
failing database is reported in the manifest without stopping the batch.
A repeated target is reported as `duplicate target` and not exported again.
Failure entries carry a fixed message (`permission denied` or
`account export failed`), never backend error text, so the manifest cannot
echo logins, emails or hashes.

| Tool | Contract |
| --- | --- |
| `users_audit` | Export one `<db>_accounts.json` per database into a fresh private run directory: `summary.last_login_by_year`, `summary.mfa_by_type`, and every active user's id/login/email/profile/stored hash. Returns a manifest of paths and counts, never accounts or hashes. |

The accounts file also contains `oauth_providers`: all configured providers
with `id`, `name`, and `enabled`, including disabled providers so old account
links remain resolvable. Each user has `oauth_provider_id` (or `null`) and
`oauth_linked` (true when both a provider and a nonempty OAuth user ID exist).
This reports stored linkage, not the last login method or proof of recent OAuth
use. OAuth is not counted as MFA. Tokens and external OAuth user IDs are never
exported. Without the OAuth schema, providers are empty and user linkage is
null/false. These details stay in the private accounts file, not the manifest.
The OAuth fields and provider schema were checked against local upstream source
for every major version from 8.0 through 20.0, plus master; they are unchanged
for these queries. This is source verification, not live database testing of
each version.

Each user's `profile` is `admin` for Administration / Settings membership
(`base.group_system`), otherwise `internal` when `share` is false, or `portal`
for external accounts (including public users). Settings takes precedence;
Administration / Access Rights alone does not make a user `admin`.
Settings membership includes transitively implied groups on every supported
version, including Odoo 19+. Odoo 8 lacks `share`, so internal access is
determined by direct or implied `base.group_user` membership there.

The files are credential material **and** personal data. Delete the run
directory once `password_hashes` and any authorized leak-check have consumed
it. Old schemas are handled: a database without `res_users_log`, `share`,
`auth_totp` or `auth_passkey` (Odoo 8 through current) still exports with the
matching summary shape. Queries are `COPY`-based CSV and catalog probes, never
`json_agg`/`json_build_object`/`to_regclass`, with recursive group traversal requiring PostgreSQL 8.4+.

## `--enable-plugins=password-audit`

This is a credential-material and local-GPU exception to the read-only
surface. Both `oa-mcp` and `oa-mcp-multi` require the operator to enable
`password-audit` at launch; installing the plugin does not enable it, and
no per-call argument can bypass the gate. `password_hashes` reads only a
`users-audit` accounts file; planning, calibration, attacks, status and
findings run on the **MCP server machine**, not the database host.

| Tool | Contract |
| --- | --- |
| `password_hashes` | Convert a `<db>_accounts.json` from `users_audit` into a fresh private hashcat target; return exact hash, derived/tier-2 wordlist and login-map paths, not hashes. No database access. |
| `password_plan` | Read exports and both companion wordlists, calibrate synthetic wordlist attacks at each actual round count, and fund passes across databases under one explicit total-minute budget. Missing companions are errors, never fabricated candidate lists. |
| `password_crack` | With a wordlist, launch a detached bounded attack; without one, only show prior audit-local cracks. Always supply explicit `minutes` when executing a plan. |
| `password_crack_status` | Poll durable running/completed/error state after launch or a disconnected call; do not launch a replacement while the pass is running. |
| `password_findings` | Return structured findings joined to the export and audit-local potfile for report aggregation; this can contain plaintext passwords. |

### Budgeted workflow

1. Run `users_audit` for the authorized databases, then call
   `password_hashes(<db>_accounts.json>)` for each and retain the exact
   returned paths.
2. Call `password_plan(audits=[{"hash_file": "<exact export path>"}], minutes=...)`.
   Calibration and input-reading time are charged to the total budget.
   Planning is capped at the smaller of that budget and 250 seconds. Probes
   reserve up to five seconds (at most half the remaining planning time)
   for returning a result. A failed probe stops calibration and returns an
   unsized tier-1-only plan whose explicit runtime caps share the remaining
   audit budget; passes with less than one second allocated are unfunded.
   These caps are allocations, not completion estimates. Input errors and
   deadline exhaustion on the measured path still return errors. Calibration
   uses `min(8, smallest actual salt-group count)` for each round count in
   the audit. The output names both the synthetic benchmark shape and the
   smallest actual count. Low-salt audits are not assigned an optimistic
   eight-salt rate; for larger counts, the measured eight-salt rate is used
   conservatively, without fabricated throughput scaling.
3. Follow funded passes in order. Each pass names its exact hash and wordlist
   paths and explicit minute/runtime cap; measured estimates are rounded up
   to whole seconds for both funding and caps; unfunded/skipped passes are not
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
