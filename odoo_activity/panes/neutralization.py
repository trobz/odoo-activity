"""Neutralization tab body -- renders odoo-db's `check-sensitive-information`
into a RichLog, led by the verdict the db row's binary tag can't carry.

The row tag answers "does this database claim to be neutralized?"
(`database.is_neutralized`, via `odoo-db list`). This tab answers the two
questions that claim leaves open, and only when a human asks for one
database -- it is a per-database read, and `oa` is often opened against a
host that is already struggling:

- what the claim did *not* clear: `live_surfaces`, the rows each module's
  own `neutralize.sql` was supposed to disable. A claimed-neutralized copy
  with an enabled payment provider is one click from charging a real card.
- what neutralization never clears at all: stored credentials. It disables
  what a database can *do*, not what it *holds*, and only for modules that
  ship a neutralize.sql -- a client's own module never does, so its API
  keys survive every neutralization intact.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text
from textual.widgets import RichLog

from odoo_activity.panes.mail import section_table


def _verdict(claimed: bool, live: int) -> Text:
    """The headline. Yellow is the case this tab exists for: the database
    says one thing and its own rows say another, which no binary tag can
    show -- treat such a copy as live until someone has read the table
    below."""
    if claimed and live:
        return Text(
            f"⚠ PARTIALLY NEUTRALIZED: database.is_neutralized is set, but {live} surface(s) below are "
            "still live -- neutralization did not finish, or something was switched back on afterwards. "
            "Treat this database as production until they are cleared.",
            style="bold yellow",
        )
    if claimed:
        return Text(
            "✓ NEUTRALIZED: database.is_neutralized is set and nothing below can still reach the outside.",
            style="bold green",
        )
    return Text(
        "✗ NOT NEUTRALIZED: database.is_neutralized is not set -- every action here is production.",
        style="bold red",
    )


def render_neutralization(body: RichLog, report: dict) -> None:
    """Populate `body` from odoo-db's `check-sensitive-information` bundle --
    same column choices as the command's own text output, so the TUI and the
    CLI tell the same story."""
    body.clear()

    surfaces = report.get("live_surfaces") or []
    params = report.get("config_parameters") or []
    servers = report.get("mail_servers") or []
    tables = report.get("candidate_tables") or []

    renderables: list[RenderableType] = [_verdict(bool(report.get("is_neutralized")), len(surfaces))]

    if surfaces:
        renderables.append(
            section_table(
                "Live external surfaces -- what neutralize should have cleared",
                ["table", "rows", "still"],
                [[s["table"], str(s["rows"]), s["reach"]] for s in surfaces],
            )
        )

    if params or servers or tables:
        # the gap the verdict above does not cover: a green database still
        # carries every secret it was copied with.
        renderables.append(
            Text(
                "Neutralization clears what a database can do, never what it holds -- the secrets below "
                "are in this copy (and in any dump of it) whatever the verdict above says.",
                style="dim",
            )
        )

    if params:
        renderables.append(
            section_table(
                f"Config parameters ({len(params)})",
                ["key", "value", "matched"],
                [[p["key"], p["value"] or "", p["marker"] or ""] for p in params],
            )
        )

    if servers:
        # "relay" earns its column: a known production relay is listed even
        # with no stored credential (it commonly authenticates by IP
        # allowlist), and without it such a row reads as an empty noise row
        # rather than as the finding it is.
        renderables.append(
            section_table(
                f"Mail servers with credentials ({len(servers)})",
                ["name", "host:port", "user", "password", "active", "relay"],
                [
                    [
                        s["name"] or "",
                        f"{s['smtp_host'] or ''}:{s['smtp_port'] or ''}",
                        s["smtp_user"] or "",
                        "set" if s["has_password"] else "",
                        "yes" if s["active"] else "no",
                        s["known_production_relay"] or "",
                    ]
                    for s in servers
                ],
            )
        )

    if tables:
        renderables.append(
            section_table(
                f"Candidate tables ({len(tables)})",
                ["table", "module", "rows", "sensitive columns"],
                [
                    [
                        t["table"],
                        t["owner_module"] or "(none)",
                        str(t["rows"]),
                        ", ".join(t["sensitive_columns"]),
                    ]
                    for t in tables
                ],
            )
        )

    body.write(Group(*renderables))
