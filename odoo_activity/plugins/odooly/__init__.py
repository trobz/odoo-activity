"""The odooly plugin: what odoo-activity can do to a database once it can
log in.

Everything here needs credentials, which is what separates it from the rest
of the app -- reading a host needs a shell on that host, logging into Odoo
needs a section in the user's own `~/odooly.ini`. So it ships as an extra
(`odoo-activity[odooly]`) and this module imports `odooly` at the top on
purpose: without the extra, the import fails, the loader skips the plugin,
and the actions that would only fail are never offered.

odooly reaches an instance over the network, so all of this runs on *this*
machine even when the instances being watched are on a remote host: `oa
openerp@somehost` still resolves envs from this machine's `~/odooly.ini`.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import odooly  # noqa: F401 -- the import *is* the availability check; see the module docstring

from odoo_activity.host import to_thread
from odoo_activity.panes.confirm import ConfirmScreen, PromptScreen
from odoo_activity.plugins import Action, DbTarget, Handler, Plugin, Target, Tool
from odoo_activity.probes import try_local_clipboard

if TYPE_CHECKING:
    from odoo_activity.tui import OdooActivity

ODOOLY_CONFIG = Path("~/odooly.ini").expanduser()

# Trobz names instances after the environment they serve, and odooly envs
# after the same thing abbreviated (or not) -- `openerp-acme18-integration`
# is configured as `acme18-integration` or `acme18-int`.
_ENV_ABBREVIATIONS = {"integration": "int", "staging": "stag", "production": "prod"}
# what a manager prefixes an instance name with, which no odooly env repeats
_INSTANCE_PREFIXES = ("openerp-", "odoo-")

# where `run_odooly_script` looks for a bare script name
_SCRIPTS = "odoo_activity.plugins.odooly.scripts"


class OdoolyEnv(TypedDict):
    """One section of `odooly.ini`: the env name, and the database it pins
    (absent when the section leaves `database` unset)."""

    name: str
    db: str


def read_odooly_envs(path: Path = ODOOLY_CONFIG) -> list[OdoolyEnv]:
    """Every environment in `path`, as (name, database).

    Read with configparser rather than `odooly.read_config`, which returns
    the password too: matching only needs the name and the database, and
    what isn't read can't be leaked into a log or a screen.

    Empty when the file is missing or unparseable -- odooly support is
    opt-in and best-effort, and a broken ini shouldn't take the app down.
    """
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(path.read_text())
    except (OSError, configparser.Error):
        return []

    return [{"name": name, "db": parser.get(name, "database", fallback="")} for name in parser.sections()]


def _name_variants(name: str) -> set[str]:
    """`name` as it may appear on either side of the match: as written, and
    with each environment word abbreviated or spelled out."""
    variants = {name}

    for long, short in _ENV_ABBREVIATIONS.items():
        variants |= {variant.replace(long, short) for variant in variants if long in variant}
        variants |= {variant.replace(short, long) for variant in variants if short in variant}

    return variants


def instance_env_name(instance_name: str) -> str:
    """`instance_name` stripped of what only a process manager adds --
    `openerp-acme18-integration.service` is the `acme18-integration` an
    odooly env would be named after."""
    name = instance_name.removesuffix(".service")

    for prefix in _INSTANCE_PREFIXES:
        name = name.removeprefix(prefix)

    return name


def match_odooly_env(instance_name: str, db: str, envs: list[OdoolyEnv]) -> str | None:
    """The odooly env serving `db` on `instance_name`, or None.

    An env qualifies when its name matches the instance's -- exactly, in
    either spelling (`-integration` / `-int`), or as that name plus a suffix,
    since a multi-db instance is usually configured one env per database
    (`acme18-int-db1`). The database has to match exactly whenever the env
    names one, which is what keeps those per-db envs apart.

    Ranked, best first: an env that names this database beats one that names
    none, and among equals the closest name wins. Ties are broken by name so
    the answer doesn't depend on the ini's ordering.
    """
    wanted = _name_variants(instance_env_name(instance_name))
    matches = []

    for env in envs:
        if env["db"] and env["db"] != db:
            continue

        names = _name_variants(env["name"])
        exact = bool(names & wanted)
        prefixed = any(name.startswith(f"{want}-") for name in names for want in wanted)
        if not (exact or prefixed):
            continue

        # a db-pinned env first, then an exact name, then the shortest suffix
        matches.append((not env["db"], not exact, len(env["name"]), env["name"]))

    return min(matches)[3] if matches else None


def _is_bare_name(script: str) -> bool:
    """Whether `script` names one of this package's own scripts rather than
    a path to a project's. `.py` is what separates them -- a bundled script
    is named without it, a discovered one always carries it."""
    return not script.endswith(".py")


def _script_label(path: Path) -> str:
    """`zones.py` -> `zones (project)`, so a project's own scripts read as
    rows rather than filenames, and are never mistaken for packaged ones."""
    return f"{path.stem.replace('_', ' ')} (project)"


def project_scripts(root: Path | None = None) -> list[Path]:
    """The `scripts/*.py` of the directory `oa` was launched from, sorted.

    A project's own scripts live in that project's repository, so running
    `oa` from a checkout is what offers them -- nothing to install, nothing
    to register. Private files (`_helpers.py`) are skipped, so a project can
    keep shared code next to the scripts that use it.
    """
    folder = (root or Path.cwd()) / "scripts"
    if not folder.is_dir():
        return []

    return sorted(path for path in folder.glob("*.py") if not path.name.startswith("_"))


def run_odooly_script(script: str, env: str, *extra_args: str, timeout: int = 300) -> str:
    """Run an odooly script against env `env`, and return what it printed
    (stdout, then stderr).

    A bare name is one of this plugin's own scripts, run as a module; a path
    is a project's own script (see `project_scripts`), run as a file so it
    keeps working whatever the working directory ends up being.

    Deliberately local and never over `Host`: odooly reaches the instance
    over the network using this machine's `~/odooly.ini`, which the watched
    host neither has nor should be asked for.

    `sys.executable` rather than a console script, so it is the interpreter
    running odoo-activity -- the one odooly is installed in -- whatever
    `PATH` says. That is also what lets a project script import this
    package's own `redact`/`use_user_config` helpers.

    `extra_args` is appended verbatim after `--env <env>`, for a script
    that needs more than the env to run (e.g. send_test_mail's `--to`).
    """
    target = ["-m", f"{_SCRIPTS}.{script}"] if _is_bare_name(script) else [script]
    argv = [sys.executable, *target, "--env", env, *extra_args]

    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return f"({script} timed out after {timeout}s)"
    except OSError as exc:
        return f"(cannot run {script}: {exc})"

    return "\n".join(part for part in (done.stdout.strip(), done.stderr.strip()) if part)


class OdoolyPlugin(Plugin):
    """odooly's contributions: a tag on every database row, the database
    Toolbox, and the two tab buttons that need a login."""

    name = "odooly"

    def __init__(self) -> None:
        # read once, at startup: the file is the user's own and small, and a
        # row's marker must not depend on when it happened to be rendered
        self.envs = read_odooly_envs()

    def _db(self, mode: str, target: Target) -> DbTarget | None:
        """`target` as an (instance, database) pair, or None outside database
        mode. The pane always pairs `mode` with the matching target shape;
        checking the shape rather than trusting `mode` is what lets the type
        follow along."""
        return target if mode == "database" and isinstance(target, tuple) else None

    def env_for(self, target: DbTarget) -> str | None:
        """The env serving this database, or None when none matches."""
        inst, db = target

        return match_odooly_env(inst["name"], db, self.envs) if self.envs else None

    def marker(self, target: DbTarget) -> str:
        """Green when a section can reach this database, red when none can.
        Both are shown -- an absent marker reads as the plugin not being
        installed, which is the one thing the tag exists to tell apart."""
        return "[green]ODOOLY[/]" if self.env_for(target) else "[red]ODOOLY[/]"

    def tools(self, mode: str, target: Target) -> list[Tool]:
        db = self._db(mode, target)
        if db is None or self.env_for(db) is None:
            return []

        rows: list[Tool] = [
            ("Open odooly (copy command)", self._copy_command),
            ("Restore app icons", self._script("restore_app_icons", "Restore app icons")),
        ]

        # a project's own scripts get the same login this database resolved
        # to, so they are offered on exactly the databases odooly can reach
        rows += [(_script_label(path), self._script(str(path), path.stem)) for path in project_scripts()]

        return rows

    def column(self, mode: str, target: Target) -> str:
        db = self._db(mode, target)
        env = self.env_for(db) if db is not None else None

        return f"odooly env: {env}" if env else ""

    def hint(self, mode: str, target: Target) -> str:
        """Every tool here logs in, so an unreachable database is told why
        rather than shown a list that would only fail."""
        db = self._db(mode, target)
        if db is None or self.env_for(db) is not None:
            return ""

        return (
            "(no odooly env for this database)\n\n"
            "Odooly actions need a section in ~/odooly.ini whose name matches this instance "
            "and whose database matches this one."
        )

    def actions(self, tab: str, target: DbTarget) -> list[Action]:
        if self.env_for(target) is None:
            return []

        if tab == "Jobs":
            return [("create-test-job", "+  Create test job", self._script("create_test_job", "Create test job"))]

        if tab == "Mail":
            return [("send-test-mail", "✉  Send test mail", self._send_test_mail)]

        return []

    async def _copy_command(self, app: OdooActivity, target: DbTarget) -> str | None:
        """Hand over the shell command rather than running it -- an odooly
        session is interactive, which a pane body is not."""
        # `-c`, because odooly's CLI looks for `odooly.ini` in the working
        # directory, not the home one -- the bare command only works if you
        # happen to paste it while sitting in the right folder
        command = f"odooly -c {ODOOLY_CONFIG} --env {self.env_for(target)}"

        if try_local_clipboard(command):
            app.notify("Copied: " + command, timeout=3)
        else:
            app.notify(command, title="Copy manually", timeout=10)

        return None

    def _script(self, script: str, label: str) -> Handler:
        """A handler that confirms, then runs `script` against the
        highlighted database's env."""

        async def run(app: OdooActivity, target: DbTarget) -> str | None:
            env = self.env_for(target)
            if env is None:
                return None

            if not await app.push_screen_wait(ConfirmScreen(f"{label} — odooly env {env}?")):
                return None

            return await to_thread(run_odooly_script, script, env)

        return run

    async def _send_test_mail(self, app: OdooActivity, target: DbTarget) -> str | None:
        """Send test mail needs a recipient, which a plain yes/no confirm
        can't collect -- PromptScreen asks for it instead, and typing an
        address in and pressing Send *is* the confirmation: there's no
        separate step after it.
        """
        env = self.env_for(target)
        if env is None:
            return None

        to = await app.push_screen_wait(PromptScreen(f"Send a real test email via odooly env {env} — to address:"))
        if not to:
            return None

        return await to_thread(run_odooly_script, "send_test_mail", env, "--to", to)
