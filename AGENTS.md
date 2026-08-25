# AGENTS.md

> Quick reference for AI coding agents.

## Project


- **Type**: CLI (Typer)

- **Language**: Python 3.10+
- **Package manager**: [uv](https://docs.astral.sh/uv/)

## Entry Points


- `odoo_activity/main.py` — CLI entry point


## Commands

Run `make help` for all commands. Key ones:

```
make install   # Install deps + pre-commit hooks
make check     # Lint, format, type-check
make test      # Run pytest

```

## Key Files

- `Makefile` — Project commands
- `pyproject.toml` — Dependencies and build config
- `ruff.toml` — Linter/formatter rules

- `tests/` — Test suite (pytest)

## Docs

[`site-docs/docs/keybindings.md`](site-docs/docs/keybindings.md)'s
keybindings table documents `odoo_activity/tui.py`'s `BINDINGS` and
`odoo_activity/panes/detail.py`'s `TABS`/`TOOLBOX_TOOLS`;
[`site-docs/docs/mcp.md`](site-docs/docs/mcp.md) documents
`odoo_activity/mcp_server.py`'s `@mcp.tool` defs. Before merging a PR that
changes any of those, run `make docs-check` (advisory — diffs the surface
files since each doc page was last touched) and fold real changes into the
relevant page.
