"""Odooly scripts — one-off maintenance actions run against a live instance.

Unlike everything else in this package, these talk to Odoo over the network
rather than reading the host: they take an `--env` naming a section of the
user's `~/odooly.ini` and connect from wherever they are run. The Toolbox
tabs shell out to them (see panes/detail.py), and they stay usable on their
own:

    python -m odoo_activity.plugins.odooly.scripts.restore_app_icons --env acme18-int
"""

from __future__ import annotations

import re

import odooly

from odoo_activity.plugins.odooly import ODOOLY_CONFIG


def use_user_config() -> None:
    """Point odooly at `~/odooly.ini`.

    Its own default is `./odooly.ini`, relative to the working directory --
    fine for a shell session started in the right place, wrong for a script
    the TUI spawns from wherever the user happened to launch it.
    """
    odooly.Client._config_file = ODOOLY_CONFIG


# `odooly.ini` documents `host = user:password@odoo.example.com`, so the
# server URL carries the password, and library errors quote what they were
# given: urllib answers a bad one with `nonnumeric port: 's3cr3t@host'`.
# These messages are printed to a terminal and into odoo-activity's pane.
_CREDENTIALS_IN_URL = re.compile(r"//[^/\s@]+:[^/\s@]+@")
# configparser quotes the line it choked on, and that line may be the secret
# itself: a `password hunter2` missing its `=` comes back verbatim.
_SECRET_SETTING = re.compile(r"\b(password|api_key)\b\s*=?\s*\S+", re.IGNORECASE)


def redact(text: str, *secrets: str) -> str:
    """`text` with `secrets` (and any user:password@ in a URL) masked.

    Secrets are matched literally because an error rarely quotes a URL
    whole -- a parser that chokes halfway prints the fragment it choked on,
    password included, and no URL-shaped pattern catches that.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")

    text = _CREDENTIALS_IN_URL.sub("//***:***@", text)

    return _SECRET_SETTING.sub(r"\1 ***", text)
