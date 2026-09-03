"""Restore the app icons a restore-without-filestore leaves blank.

`web_icon_data` is a stored compute over the icon file in the filestore, so
a database restored without one shows an apps menu full of empty tiles. The
fix is to write `web_icon` back onto every menu that has one, which makes
Odoo recompute the data from the module's own image.

Ported from emoi's `restore_app_icons_-_from_api.py`, with two changes: the
CLI is typer, to match the rest of this package, and only the menus whose
icon is actually missing are rewritten (see restore_app_icons).

    python -m odoo_activity.plugins.odooly.scripts.restore_app_icons --env acme18-int
"""

from __future__ import annotations

import sys

import odooly
import typer

from odoo_activity.plugins.odooly.scripts import redact, use_user_config

app = typer.Typer(add_completion=False)


def restore_app_icons(client: odooly.Client) -> tuple[int, int]:
    """Rewrite `web_icon` on the menus whose icon data is missing, and return
    (restored, considered).

    Only the broken ones: writing `web_icon` back is what makes Odoo
    recompute `web_icon_data`, and doing that to a menu whose icon is fine
    would rewrite a good attachment, orphan its file in the filestore and
    bump the record for nothing. Running twice in a row is then a no-op the
    second time.

    `web_icon_data` can't be filtered on in the domain -- it is a binary
    stored as an attachment, which Odoo doesn't make searchable -- so the
    icons are read and checked here. A lost filestore leaves the attachment
    row in place and only the file missing, and `_file_read` answers that
    with empty bytes, so reading is also the only way to see it.
    """
    menus = client.env["ir.ui.menu"].search_read(
        [["web_icon", "!=", False]], ["id", "name", "web_icon", "web_icon_data"]
    )
    broken = [menu for menu in menus if not menu["web_icon_data"]]

    for menu in broken:
        print(f"- {menu['name']}")
        client.env["ir.ui.menu"].write(menu["id"], {"web_icon": menu["web_icon"]})

    return len(broken), len(menus)


@app.command()
def main(env: str = typer.Option(..., "--env", help="Section of ~/odooly.ini to connect with")) -> None:
    """Restore the app icons of the instance `--env` points at."""
    use_user_config()
    password = ""

    try:
        # read the config first: knowing the password is what lets the error
        # below be printed without it, since the server URL may embed it
        password = odooly.read_config(env)[3] or ""
        client = odooly.Client.from_config(env)
    except Exception as exc:  # a missing section, or a server that won't answer
        typer.echo(f"cannot connect to '{env}': {redact(str(exc), password)}", err=True)
        raise typer.Exit(1) from exc

    print(f"Restoring app icons on {env}:")
    restored, considered = restore_app_icons(client)

    if not considered:
        print("Nothing to restore — no menu declares an icon.")
    elif not restored:
        print(f"Nothing to restore — all {considered} menu icon(s) are already there.")
    else:
        print(f"Complete — {restored} of {considered} menu icon(s) restored.")


if __name__ == "__main__":
    sys.exit(app())
