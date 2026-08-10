# CHANGELOG

<!-- version list -->

## v0.12.0 (2026-08-10)

### Features

- **probes**: Detect directly-run local instances
  ([`b9e62f3`](https://github.com/trobz/odoo-activity/commit/b9e62f3e295ad8bdef335d7ec335ad5436cfbbf9))


## v0.11.0 (2026-08-07)

### Bug Fixes

- **probes**: Resolve bare interpreter in shell_command
  ([`65d11db`](https://github.com/trobz/odoo-activity/commit/65d11dbc266e25662948b4a02201a02425fba189))

### Documentation

- Update keybindings table and add docs-check make target
  ([`f7f4d35`](https://github.com/trobz/odoo-activity/commit/f7f4d3513a67d9bf08885d6daf13e1d520dc1232))

### Features

- **activity-pane**: Add Summary tab for workers by role
  ([`9a44fbf`](https://github.com/trobz/odoo-activity/commit/9a44fbf03c13434a3c448b8fa6e0d7e370ddd399))

- **mcp**: Add instance_shell_command tool
  ([`3ab75df`](https://github.com/trobz/odoo-activity/commit/3ab75df08e2368ff47cddd42ed5a2b6711e8696f))

- **toolbox**: Add open shell command copy and session counter
  ([`41b149c`](https://github.com/trobz/odoo-activity/commit/41b149c8a215c0dad7b01c999e72890beb578459))

### Refactoring

- Rename Processes tab to Top
  ([`a12c4d3`](https://github.com/trobz/odoo-activity/commit/a12c4d3928c36c147f4df38912205b75d12f4fab))

- Rename Summary tab to Processes
  ([`5093f19`](https://github.com/trobz/odoo-activity/commit/5093f193e086689773c8d761c26d3c1ce1673f8e))

- **toolbox**: Move session count off C into Toolbox
  ([`145d668`](https://github.com/trobz/odoo-activity/commit/145d6687a674a6e27461ddf3688ae99babbd9a0b))


## v0.10.0 (2026-08-04)

### Bug Fixes

- **activity-pane**: Focus the active tab body on every tab switch
  ([`0e729ec`](https://github.com/trobz/odoo-activity/commit/0e729ec09fe41cf2abab3edb105efb974c4a51a6))

### Features

- **activity-pane**: Add Toolbox tab for worker scaling
  ([`7aa774a`](https://github.com/trobz/odoo-activity/commit/7aa774a6d5d6ed74707e6cbbb60fd3ddc47fb1cd))


## v0.9.0 (2026-07-29)

### Bug Fixes

- **tui**: Show request context in stack-dump thread display
  ([`3d98012`](https://github.com/trobz/odoo-activity/commit/3d98012ca38c86008a5bd515d82686df9c410801))

### Features

- **mcp**: Add read-only diagnostic tools
  ([`093caad`](https://github.com/trobz/odoo-activity/commit/093caada2e38f84b1ee8d1e872cb199989f252ab))


## v0.8.0 (2026-07-29)

### Bug Fixes

- **ssh**: Graceful cleanup of ControlMaster on quit + UI focus fix
  ([`8436d12`](https://github.com/trobz/odoo-activity/commit/8436d12795dec983684ae99373b659e21cd9168c))

- **tui**: Guard host-stats refresh against teardown race
  ([`dd512e2`](https://github.com/trobz/odoo-activity/commit/dd512e29d19d015b8ec6af5461be9364df91ea18))

### Documentation

- Cover MCP server (oa-mcp and oa-mcp-multi)
  ([`5298916`](https://github.com/trobz/odoo-activity/commit/5298916b740a380eb406ba96e2eba78c5b06e392))

- Cover ssh remote mode
  ([`3e29bfe`](https://github.com/trobz/odoo-activity/commit/3e29bfe5497aea8ec7d315b6f5229b6f218fc3c2))

### Features

- **mcp**: Add oa-mcp-multi with host-filter and list_hosts
  ([`c714bd8`](https://github.com/trobz/odoo-activity/commit/c714bd888a6df6ec81a32967048c61bd99c07300))

- **mcp**: Pin oa-mcp to a single host like oa [host]
  ([`12e5c13`](https://github.com/trobz/odoo-activity/commit/12e5c13524305e3b67125c88d3fc5039509a222a))

- **mcp**: Probe a remote host from the mcp tools
  ([`cc29dcc`](https://github.com/trobz/odoo-activity/commit/cc29dccd13fdb6331570ab9d44feb6dc31125caf))

- **ssh**: Watch a remote host over ssh
  ([`cab391c`](https://github.com/trobz/odoo-activity/commit/cab391c7794bbfd6ca75586d1e04a13aa3c50ade))


## v0.7.0 (2026-07-27)

### Features

- **mcp**: Add oa-mcp POC with read-only instance/process tools
  ([`5621100`](https://github.com/trobz/odoo-activity/commit/56211008cba4e70960ddd36f9f14ba7c197bc0bb))

### Refactoring

- **probes**: Extract status logic to probes module
  ([`faaacbe`](https://github.com/trobz/odoo-activity/commit/faaacbe86ab337040f7e9464545d8650dc124117))

- **probes**: Type Instance and ProcRow throughout the TUI
  ([`b142d02`](https://github.com/trobz/odoo-activity/commit/b142d02f7a5853553286dfa177e2f9d0ef9f298d))


## v0.6.0 (2026-07-24)

### Features

- **stacks**: Cache stack dumps per instance when switching
  ([`712d56c`](https://github.com/trobz/odoo-activity/commit/712d56c4fa374798cf8b258779f7e6391246b168))

- **tui**: Add Stacks pane for parsed dumpstacks output
  ([`0fb1365`](https://github.com/trobz/odoo-activity/commit/0fb1365ff8cb5ff34a4e8c6d166844439eeda63f))

- **tui**: Show crons' code, wrapped instead of clipped
  ([`36c142d`](https://github.com/trobz/odoo-activity/commit/36c142d73d73e429b786c73478f112ba391f9c01))

- **tui**: Use tighter color thresholds for the swap bar
  ([`34aec01`](https://github.com/trobz/odoo-activity/commit/34aec019ee08167f3a33161609ccc40e7fc22c04))


## v0.5.0 (2026-07-20)

### Documentation

- **readme**: Document odoo.sh manager and new keybindings
  ([`76e4c18`](https://github.com/trobz/odoo-activity/commit/76e4c1823a08703828dc4fb0e6bba15d27d9321e))

### Features

- **tui**: Add odoo.sh instance support
  ([`8773ac2`](https://github.com/trobz/odoo-activity/commit/8773ac2bf7cc1dee17eadce69bd8e4818859c43d))


## v0.4.0 (2026-07-10)

### Features

- **tui**: Add Queries tab and pane maximize
  ([`fe74367`](https://github.com/trobz/odoo-activity/commit/fe743670580263b0a1b78eb4af404ff048a3b1a8))


## v0.3.0 (2026-07-08)

### Features

- **tui**: Optimize process fetching, preserve table scroll, improve UI format
  ([`740ba9e`](https://github.com/trobz/odoo-activity/commit/740ba9e134afdda0e5564fa926517919f0c6972d))


## v0.2.0 (2026-07-08)

### Features

- **tui**: Add Processes and Config tabs, support process signals
  ([`4fd17fc`](https://github.com/trobz/odoo-activity/commit/4fd17fc8ebd876eb3f42ba5f5fb8e2eca454f7c3))


## v0.1.0 (2026-07-07)

### Bug Fixes

- **tui**: Give trobz theme's accent its own color
  ([`c50bf66`](https://github.com/trobz/odoo-activity/commit/c50bf6651d851e0a042c75421125500e11d0f9fe))

- **tui**: Stop db-tab switches from hanging behind slow queries
  ([`998b390`](https://github.com/trobz/odoo-activity/commit/998b3900b7456abf47c68e52af00cf9dba57f855))

- **tui**: Use primary color for focused borders and accent for unfocused
  ([`3671bc5`](https://github.com/trobz/odoo-activity/commit/3671bc5fa06043aecc09f2de5865670f9d8378f9))

### Features

- **panel**: Add odoo-activity TUI
  ([`aafe406`](https://github.com/trobz/odoo-activity/commit/aafe406042b2852386bc2bf19ea5cb82144e5c66))

- **tui**: Trobz theme, swap panel, and context-aware tab keys
  ([`bdf7a07`](https://github.com/trobz/odoo-activity/commit/bdf7a079bfd63e5b95a52ce5975d0f3b95c11fa1))


## v0.0.0 (2026-07-03)

- Initial Release
