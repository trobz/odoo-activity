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

# odoo-db's own three states, and the words it prints for each -- kept
# identical so the tab and `odoo-db check-sensitive-information` say the
# same thing about the same database.
_VERDICTS = {
    "neutralized": (
        "✓ NEUTRALIZED: database.is_neutralized is set and nothing below can still reach the outside.",
        "bold green",
    ),
    "partial": (
        "⚠ PARTIALLY NEUTRALIZED: database.is_neutralized is set, but the surfaces below are still live "
        "-- neutralization did not finish, or something was switched back on afterwards. Treat this "
        "database as production until they are cleared.",
        "bold yellow",
    ),
    "not_neutralized": (
        "✗ NOT NEUTRALIZED: database.is_neutralized is not set -- every action here is production.",
        "bold red",
    ),
}


def _state(report: dict) -> str:
    """odoo-db's verdict, or the same rule applied here.

    `state` is odoo-db's (`neutralization_state`): the claim and the
    surfaces have to agree before a database reads clean. A host whose
    odoo-db predates that key falls back to deriving it -- same
    graceful-degradation convention `panes/mail.py` follows for
    `is_test_catcher`, so an older tool loses the shared wording, not the
    tab.

    The fallback is deliberately the weaker half of odoo-db's rule: without
    `state` there is no `surfaces` either, so an uncountable surface can't
    be told from a clean one here and only `live_surfaces` is left to go on.
    """
    state = report.get("state")
    if state in _VERDICTS:
        return state
    if not report.get("is_neutralized"):
        return "not_neutralized"
    return "partial" if report.get("live_surfaces") else "neutralized"


def _verdict(report: dict) -> Text:
    """The headline. Yellow is the case this tab exists for: the database
    says one thing and its own rows say another, which no binary tag can
    show -- treat such a copy as live until someone has read the table
    below."""
    message, style = _VERDICTS[_state(report)]
    return Text(message, style=style)


def render_neutralization(body: RichLog, report: dict) -> None:
    """Populate `body` from odoo-db's `check-sensitive-information` bundle --
    same column choices as the command's own text output, so the TUI and the
    CLI tell the same story."""
    body.clear()

    surfaces = report.get("live_surfaces") or []
    params = report.get("config_parameters") or []
    servers = report.get("mail_servers") or []
    tables = report.get("candidate_tables") or []

    renderables: list[RenderableType] = [_verdict(report)]

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
