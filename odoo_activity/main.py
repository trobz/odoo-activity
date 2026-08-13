import logging
import logging.handlers
import os
import queue
import traceback
from pathlib import Path

import typer

from odoo_activity.host import Host, close_control_master
from odoo_activity.tui import OdooActivity

app = typer.Typer(add_completion=False)

DEBUG_LOG_PATH = Path.home() / ".oa-debug.log"


def _setup_debug_logging() -> None:
    # Use non-blocking queue logging to prevent disk I/O latency from delaying keypresses.
    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    logging.getLogger().addHandler(logging.handlers.QueueHandler(log_queue))
    logging.getLogger().setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(DEBUG_LOG_PATH)
    # pid: the path is fixed, so concurrent runs append here and interleave
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(process)d] %(levelname)s %(message)s"))
    logging.handlers.QueueListener(log_queue, file_handler).start()


@app.command()
def main(
    host: str | None = typer.Argument(None, help="[user@]host to watch over ssh, e.g. openerp@demo. Omit for local."),
    port: int | None = typer.Option(None, "--port", "-p", help="ssh port, for a non-default one."),
    debug: bool = typer.Option(
        False, "--debug", help=f"log keypress/loop-lag diagnostics to {DEBUG_LOG_PATH} (tail -f it from elsewhere)."
    ),
    include_sensitive_information: bool = typer.Option(
        True,
        "--include-sensitive-information/--no-include-sensitive-information",
        help="Show db-tab params' secret-looking values unmasked. On by default -- you're a human with a "
        "shell on this host already; --no-include-sensitive-information keeps odoo-db's own masking.",
    ),
) -> None:
    """odoo-activity — TUI for local or ssh-remote Odoo instances."""
    if debug:
        _setup_debug_logging()

    target = Host(alias=host, port=port)
    code = 0
    try:
        OdooActivity(host=target, include_sensitive_information=include_sensitive_information).run()
    except BaseException:
        traceback.print_exc()
        code = 1

    # graceful-quit-only: a hard kill never runs this, ControlPersist=600
    # reaps that case on its own past its 10min idle window.
    close_control_master(target)

    # concurrent.futures' atexit joins probe threads, so an in-flight ssh call
    # holds the process open with the terminal already restored. os._exit skips
    # it -- and must cover the raising paths too, since ^C escapes .run().
    # See host.to_thread for the other half, inside .run().
    os._exit(code)


if __name__ == "__main__":
    app()
