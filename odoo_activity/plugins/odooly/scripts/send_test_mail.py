"""Send one real test email through mail.mail, to check outbound mail from an
instance actually reaches an inbox rather than just queuing.

The `from` address is the connecting user's own company email -- the same
address a real notification from this login would carry -- so nothing but
the recipient needs asking. Calling `.send()` directly, rather than leaving
the record `outgoing` for the mail queue cron to pick up, is what makes this
synchronous -- the result is known by the time this exits rather than
"maybe, eventually".

    python -m odoo_activity.plugins.odooly.scripts.send_test_mail --env acme18-int --to me@example.com
"""

from __future__ import annotations

import sys

import odooly
import typer

from odoo_activity.plugins.odooly.scripts import redact, use_user_config

app = typer.Typer(add_completion=False)

SUBJECT = "This is a test email from Trobz"
BODY_HTML = "Please ignore"


def _company_email(client: odooly.Client) -> str | None:
    """The connecting user's own company email, via two plain search_reads
    (no relational-field wrapping to second-guess -- see restore_app_icons.py
    for the same style). None if the company has none set."""
    users = client.env["res.users"].search_read([["id", "=", client.env.uid]], ["company_id"])
    company = users[0]["company_id"] if users else None
    if not company:
        return None

    companies = client.env["res.company"].search_read([["id", "=", company[0]]], ["email"])
    return companies[0]["email"] if companies else None


def send_test_mail(client: odooly.Client, to: str) -> int:
    """Create and send one `mail.mail` to `to`, returning its id.

    odooly's `create()` already returns the created `Record` (it calls
    `browse()` internally) rather than a plain id -- browsing it again, as
    this used to, passes a `Record` where odooly's `RecordList.__init__`
    asserts an `int`, raising `AssertionError: <Record 'mail.mail,N'>` on
    every real send. Caught live: login and creation both succeeded against
    a real host, so this only ever surfaced at the final `.send()` step.

    `email_from` is only set in `values` when `_company_email()` actually
    returns one -- passing the key with `None` is not the same as omitting
    it: `_add_missing_default_values`/`mail.message.default_get` only fill
    in a field that's *absent* from `values`, so an explicit `None` there
    suppresses Odoo's own sender fallback and the mail goes out with no
    `from` address at all, which the SMTP layer then rejects.

    `mail.send()` defaults to `raise_exception=False`: a failed send is
    recorded on the mail as `state='exception'` + `failure_reason`, never
    raised as a Python exception -- `auto_delete: False` above keeps the
    record around specifically so it can be read back here. Without this,
    every failure would still return normally and be reported as "sent",
    which is the one outcome this whole script exists to catch.
    """
    values = {
        "email_to": to,
        "subject": SUBJECT,
        "body_html": BODY_HTML,
        "auto_delete": False,
    }
    email_from = _company_email(client)
    if email_from:
        values["email_from"] = email_from

    mail = client.env["mail.mail"].create(values)
    mail.send()

    sent = client.env["mail.mail"].search_read([["id", "=", mail.id]], ["state", "failure_reason"])
    result = sent[0] if sent else {}
    if result.get("state") == "exception":
        raise RuntimeError(result.get("failure_reason") or "the mail was not sent, with no reason recorded")
    return mail.id


@app.command()
def main(
    env: str = typer.Option(..., "--env", help="Section of ~/odooly.ini to connect with"),
    to: str = typer.Option(..., "--to", help="Recipient address"),
) -> None:
    """Send a test email on the instance `--env` points at, to `--to`."""
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

    try:
        mail_id = send_test_mail(client, to)
    except Exception as exc:
        typer.echo(f"could not send test mail on '{env}': {redact(str(exc), password)}", err=True)
        raise typer.Exit(1) from exc

    print(f"Test mail sent on {env}: mail.mail #{mail_id}, to {to}")


if __name__ == "__main__":
    sys.exit(app())
