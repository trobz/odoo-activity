"""Queue one of queue_job's own test jobs, to see whether a runner picks it up.

The job is created through queue_job's `/queue_job/create_test_job`
controller rather than over RPC: the controller calls
`env["queue.job"].with_delay()._test_job()`, and neither half of that is
reachable from a client — `with_delay` is a python-level API, and `_test_job`
is private, which Odoo refuses to expose. So odooly is used for what it is
good for here (resolving `--env` to a server, database and credentials) and
the two calls the controller needs go over plain HTTP.

    python -m odoo_activity.plugins.odooly.scripts.create_test_job --env acme18-int
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse, urlunparse

import odooly
import requests
import typer

from odoo_activity.plugins.odooly.scripts import redact, use_user_config

app = typer.Typer(add_completion=False)

TIMEOUT = 30
_LOGIN_REFUSED = "login refused — check the credentials in ~/odooly.ini"


def _base_url(server: str) -> tuple[str, tuple[str, str] | None]:
    """The instance's root plus basic-auth credentials, from whatever odooly
    resolved `--env` to. Its `server` is an endpoint
    (`https://user:password@host/xmlrpc`), not a site root, and the
    controllers below hang off the root. Credentials in the URL would make
    requests resolve them as a hostname (`Name or service not known`), so
    they are pulled out and carried as HTTP basic auth instead — some hosts
    (staging boxes behind an nginx gate) also demand it on their own."""
    parts = urlparse(server)
    base_url = urlunparse((parts.scheme, parts.netloc, "", "", "", ""))

    return base_url, ((parts.username or "", parts.password or "") if parts.username or parts.password else None)


def create_test_job(base_url: str, db: str, login: str, password: str, auth: tuple[str, str] | None) -> str:
    """Log in, ask queue_job for a test job, and return its uuid."""
    session = requests.Session()
    if auth:
        session.auth = auth

    answer = session.post(
        f"{base_url}/web/session/authenticate",
        json={"jsonrpc": "2.0", "method": "call", "params": {"db": db, "login": login, "password": password}},
        timeout=TIMEOUT,
    )
    answer.raise_for_status()
    answer = answer.json()

    # a failed login is a 200 with an `error` member, not an HTTP error
    if "error" in answer or not answer.get("result", {}).get("uid"):
        raise RuntimeError(_LOGIN_REFUSED)

    # GET, not POST: older queue_job routes the controller as http GET only,
    # and answers a POST with a bare 400
    response = session.get(f"{base_url}/queue_job/create_test_job", timeout=TIMEOUT)
    response.raise_for_status()

    # older queue_job answers with the bare uuid, newer with "job uuid: <uuid>"
    return response.text.replace("job uuid: ", "").strip()


@app.command()
def main(env: str = typer.Option(..., "--env", help="Section of ~/odooly.ini to connect with")) -> None:
    """Queue a test job on the instance `--env` points at."""
    try:
        use_user_config()
        server, db, login, password, _api_key = odooly.read_config(env)
    except Exception as exc:  # a missing section reads as a config error
        # the password isn't known yet, but a parse error quotes the line it
        # choked on -- which may be that very setting (see redact())
        typer.echo(f"no such odooly env: '{env}' ({redact(str(exc))})", err=True)
        raise typer.Exit(1) from exc

    if not password:
        typer.echo(f"'{env}' has no password in ~/odooly.ini, and this can't prompt for one", err=True)
        raise typer.Exit(1)

    server = server if isinstance(server, str) else server[0]

    try:
        base_url, auth = _base_url(server)
        uuid = create_test_job(base_url, db, login, password, auth)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        # the server URL may embed the password -- see redact()
        typer.echo(f"could not create a test job on '{env}': {redact(str(exc), password)}", err=True)
        raise typer.Exit(1) from exc

    print(f"Test job queued on {env}: {uuid}")


if __name__ == "__main__":
    sys.exit(app())
