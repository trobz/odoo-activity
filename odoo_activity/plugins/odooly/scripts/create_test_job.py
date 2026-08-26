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

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import odooly
import typer

from odoo_activity.plugins.odooly.scripts import redact, use_user_config

app = typer.Typer(add_completion=False)

TIMEOUT = 30
_LOGIN_REFUSED = "login refused — check the credentials in ~/odooly.ini"


def _base_url(server: str) -> str:
    """The instance's root, from whatever odooly resolved `--env` to: its
    `server` is an endpoint (`https://host/jsonrpc`), not a site root, and
    the controllers below hang off the root."""
    parts = urllib.parse.urlsplit(server)

    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def create_test_job(base_url: str, db: str, login: str, password: str) -> str:
    """Log in, ask queue_job for a test job, and return its uuid."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"db": db, "login": login, "password": password},
    }).encode()
    request = urllib.request.Request(  # noqa: S310 -- scheme comes from odooly's own config
        f"{base_url}/web/session/authenticate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    with opener.open(request, timeout=TIMEOUT) as response:
        answer = json.loads(response.read())

    # a failed login is a 200 with an `error` member, not an HTTP error
    if "error" in answer or not answer.get("result", {}).get("uid"):
        raise RuntimeError(_LOGIN_REFUSED)

    with opener.open(f"{base_url}/queue_job/create_test_job", timeout=TIMEOUT) as response:
        # older queue_job answers with the bare uuid, newer with "job uuid: <uuid>"
        return response.read().decode().replace("job uuid: ", "").strip()


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

    try:
        uuid = create_test_job(_base_url(server), db, login, password)
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        # the server URL may embed the password -- see redact()
        typer.echo(f"could not create a test job on '{env}': {redact(str(exc), password)}", err=True)
        raise typer.Exit(1) from exc

    print(f"Test job queued on {env}: {uuid}")


if __name__ == "__main__":
    sys.exit(app())
