"""Mail tab body -- renders odoo-db's `mail` audit as separate tables per
section into a RichLog, instead of flattening everything into one
DataTable tagged by a `section` column: the sections don't share columns
(config parameters vs. mail servers vs. modules), so mashing them together
under the generic table renderer read as mostly blank cells -- caught live
against a real host, flagged by a reviewer looking at the screenshot."""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.widgets import RichLog

_DEFAULT_WARNING = "  [WARNING: still Odoo default -- update it]"
_SECRET_MASK = "********"  # noqa: S105 -- mirrors odoo_db.db._SECRET_MASK, mask placeholder not a
# real secret; odoo-db masks these unless --include-sensitive-information was passed (the TUI's
# own default, user-togglable).


def _mail_server_creds_cell(user: str | None, pwd: str | None) -> str:
    """One column instead of two: masked, user/password each carry only
    "is something set", at the cost of the widest cells for one bit of
    information each. Detected by the literal
    mask string rather than a passed-in flag -- render_mail only ever sees
    already-fetched data, not the reveal flag that produced it -- so real,
    revealed values (an admin explicitly asked to see them) still show in
    full rather than being collapsed to "set". A field only ever equals
    the mask string when it was actually set (odoo-db masks a non-empty
    value, never fabricates one), so this never has to distinguish "set"
    from "unset" once masked -- either field matching is enough."""
    if user == _SECRET_MASK or pwd == _SECRET_MASK:
        return "set"
    return "/".join(filter(None, (user, pwd)))


# `host:port` and `creds` hold single unbreakable tokens, so rich's default
# `ellipsis` overflow drops characters off them as soon as the pane is
# narrow -- a 120-column terminal is already enough (`smtp.sendg…`,
# `invalid:10…`), and a truncated hostname is exactly the value this tab is
# read for. folding wraps them mid-token instead: uglier, but nothing is
# lost. Not applied to every column: the prose-shaped ones wrap on word
# boundaries already, which reads better than a mid-word fold.
_FOLDED_COLUMNS = frozenset({"host:port", "creds"})


def _section_table(title: str, columns: list[str], rows: list[list[str]]) -> Table:
    table = Table(title=title, title_style="bold", title_justify="left", show_lines=False, expand=False)
    for col in columns:
        table.add_column(col, overflow="fold" if col in _FOLDED_COLUMNS else "ellipsis")
    for row in rows:
        table.add_row(*row)
    return table


def _mail_servers_section(mail_servers: list[dict]) -> list[RenderableType]:
    """The mail_servers table plus its summary lines (test catcher, known
    relay, neutralization stub) -- split out of render_mail to keep it under
    the complexity limit; also the one section always shown (see its
    caller)."""
    if not mail_servers:
        return [Text("Outgoing mail servers: (none defined -- odoo will use localhost:25)")]

    # .get(...): a host whose odoo-db predates is_test_catcher/
    # known_production_relay/is_neutralization_stub (see get_mail_servers)
    # simply doesn't flag anything, same graceful-degradation convention the
    # --all flag uses elsewhere -- caught live: a hard index here crashed
    # the whole tab (KeyError) against a real host running an older odoo-db.
    rows = [
        [
            str(m["sequence"]) if m["sequence"] is not None else "",
            m["name"],
            f"{m['smtp_host'] or ''}:{m['smtp_port'] if m['smtp_port'] is not None else ''}",
            _mail_server_creds_cell(m["smtp_user"], m["smtp_pass"]),
            " / ".join(filter(None, (m["smtp_encryption"], m["smtp_authentication"]))),
            m["from_filter"] or "",
            "yes" if m["active"] else "no",
        ]
        for m in mail_servers
    ]
    renderables: list[RenderableType] = [
        _section_table(
            "Outgoing mail servers",
            ["seq", "name", "host:port", "creds", "encryption/auth", "from_filter", "active"],
            rows,
        )
    ]
    # Named, not "server(s)": with several relays on one database, a bare
    # "server(s) above" forces the reader to guess which row it means.
    # Archived (active=False) relays are excluded -- they swallow nothing,
    # so flagging one reads as a false alarm.
    # sorted(set(...)): two relays at the same provider would otherwise
    # repeat its name once per row.
    catchers = sorted({m["name"] for m in mail_servers if m.get("is_test_catcher") and m["active"]})
    if catchers:
        # The self-diagnosis this whole tab exists for: "why doesn't my
        # email arrive" reads as a config/network mystery unless this is
        # spelled out -- a test catcher accepts mail and Odoo marks it sent
        # with no error, indistinguishable from a working relay anywhere
        # else in the UI (verified live: a real test send through mailhog
        # landed in its own catch-all, not an inbox).
        renderables.append(
            Text(
                f"⚠ WARNING: {', '.join(catchers)} -- test-mail catcher(s): accept mail but never relay it "
                "anywhere real, so a real send never reaches a real inbox from here, by design.",
                style="bold red",
            )
        )
    known_relays = sorted({
        m["known_production_relay"] for m in mail_servers if m.get("known_production_relay") and m["active"]
    })
    if known_relays:
        # The positive counterpart: "not flagged as a test catcher" is an
        # absence, not a confirmation -- this is one.
        renderables.append(
            Text(
                f"✓ [KNOWN RELAY] confirmed against: {', '.join(known_relays)} -- a real managed relay.",
                style="bold green",
            )
        )
    stubs = sorted({m["name"] for m in mail_servers if m.get("is_neutralization_stub")})
    if stubs:
        renderables.append(
            Text(
                f"[NEUTRALIZATION STUB] {', '.join(stubs)} -- Odoo's own db_neutralize placeholder, not a "
                "real relay, and not the cause of mail failing on its own.",
                style="bold yellow",
            )
        )
    return renderables


def render_mail(body: RichLog, audit: dict) -> None:
    """Populate `body` with one Rich Table per non-empty section of
    odoo-db's `mail` audit bundle -- same column choices and None/default
    handling as `odoo-db mail`'s own text output, so the TUI and the CLI
    tell the same story."""
    body.clear()
    renderables: list[RenderableType] = []

    if audit.get("is_neutralized"):
        # database.is_neutralized (set by base/data/neutralize.sql, every
        # odoo.sh staging build) is the single most common reason mail never
        # leaves an Odoo database -- surfaced here so a reader doesn't have
        # to piece it together from an active=no relay and an unfamiliar
        # stub row below (see is_neutralization_stub).
        renderables.append(
            Text(
                "⚠ DATABASE IS NEUTRALIZED: outgoing mail is disabled by an Odoo-inserted stub relay "
                "below -- any other relay listed as inactive was disabled by neutralization, not "
                "misconfiguration.",
                style="bold red",
            )
        )

    renderables += _mail_servers_section(audit.get("mail_servers") or [])

    params = audit.get("config_parameters") or []
    if params:
        rows = [
            [
                p["key"] + (f" ({p['explanation']})" if p.get("explanation") else ""),
                "(not defined)" if p["value"] is None else p["value"],
            ]
            for p in params
        ]
        renderables.append(_section_table("Config parameters", ["key", "value"], rows))

    alias_domains = audit.get("alias_domains")
    if alias_domains:
        # .get(...): an older odoo-db's bundle lacks this key entirely --
        # same graceful-degradation convention as is_test_catcher/
        # is_neutralization_stub -- so a missing alias domain there just
        # reads as the plain, non-alarming case rather than crashing.
        legacy_configured = audit.get("is_legacy_mail_config_configured")
        rows = [
            [
                a["company_name"],
                a["alias_domain"]
                or (
                    "NOT SET despite legacy ICP config still present -- stuck v16-to-17 migration!"
                    if legacy_configured
                    else "(not set -- normal for a clean 17+ install)"
                ),
                a["bounce_email"] or "",
                a["catchall_email"] or "",
                a["default_from_email"] or "",
            ]
            for a in alias_domains
        ]
        renderables.append(
            _section_table(
                "Alias domains (Odoo 17+, authoritative)",
                ["company", "alias_domain", "bounce_email", "catchall_email", "default_from_email"],
                rows,
            )
        )

    addresses = audit.get("addresses") or []
    if addresses:
        rows = []
        for a in addresses:
            # .get(...): an older odoo-db's bundle lacks this key entirely
            # -- same graceful-degradation convention as is_test_catcher --
            # so a row from it just falls through to the plain "(not set)"/
            # real-email cases below instead of crashing.
            if a.get("missing"):
                email = "(record missing)"
            else:
                email = "(not set)" if a["email"] is None else a["email"]
                if a["is_default"]:
                    email += _DEFAULT_WARNING
            partner_id = "" if a["partner_id"] is None else str(a["partner_id"])
            rows.append([partner_id, a["label"], email])
        renderables.append(_section_table("Relevant addresses", ["partner_id", "label", "email"], rows))

    modules = audit.get("modules") or []
    if modules:
        renderables.append(
            _section_table("Relevant modules", ["module", "state"], [[m["name"], m["state"]] for m in modules])
        )

    # renderables is never empty: the mail_servers branch above always
    # contributes something, table or placeholder text.
    body.write(Group(*renderables))
