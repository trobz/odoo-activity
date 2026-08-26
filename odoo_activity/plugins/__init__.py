"""Plugin discovery, and the contract a plugin implements.

A plugin *contributes* to an instance core already found: rows in a Toolbox,
an action button under a database tab, a tag on a database row. Everything it
offers is optional -- one that isn't installed leaves the app exactly as it
was, which is what lets a feature like odooly ship as an extra rather than as
a hard dependency.

Discovery is `importlib.metadata` over the `odoo_activity.plugins` entry
point group. The bundled plugins register through that same group in this
project's own pyproject, so there is one loader path and the shipped plugin
goes through the same door a third-party one would.

Managers -- what an instance *is* and how to reach it (systemd, supervisor,
odoo.sh) -- are a different contract and live in probes.py. Listing
instances is the smallest part of that job: whoever finds an instance also
owns how its log is read, how its config is found and how its databases are
reached, none of which this interface can express.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from odoo_activity.probes import Instance
    from odoo_activity.tui import OdooActivity

GROUP = "odoo_activity.plugins"
MANAGER_GROUP = "odoo_activity.managers"

# what a contribution acts on: an instance row, or one database on one
DbTarget = tuple["Instance", str]
Target = Union["Instance", DbTarget]

# A handler gets the app -- so it can confirm, prompt, notify, or reach a
# probe -- and returns what to show in the pane body, or None when it has
# already said its piece (a clipboard copy notifies and shows nothing).
# Database-mode only, because that is the only mode that runs one today.
Handler = Callable[["OdooActivity", DbTarget], Awaitable[str | None]]

Tool = tuple[str, Handler]  # a Toolbox row: (label, handler)
Action = tuple[str, str, Handler]  # a tab's button: (id, label, handler)


class Plugin:
    """Base class for a plugin: every hook is a no-op, so a subclass writes
    only the ones it uses.

    `mode` is "instance" or "database" and `tab` is the tab's label, both as
    the pane knows them; `target` is the highlighted instance, or the
    (instance, database) pair in database mode.
    """

    name = ""

    def marker(self, target: DbTarget) -> str:
        """A short tag for this database's row in the instances list, or ""
        for no tag at all."""
        return ""

    def tools(self, mode: str, target: Target) -> list[Tool]:
        """Rows to add to the Toolbox tab."""
        return []

    def column(self, mode: str, target: Target) -> str:
        """What this plugin's rows are acting through, for the Toolbox
        column header -- odooly names the env, so it is visible *which*
        credentials the rows would use before pressing one."""
        return ""

    def hint(self, mode: str, target: Target) -> str:
        """Why the Toolbox is empty, when this plugin could have filled it
        but can't reach this target. Shown only when no plugin offered a
        row -- a plugin that simply doesn't apply here says nothing."""
        return ""

    def actions(self, tab: str, target: DbTarget) -> list[Action]:
        """Buttons to add to the strip under a database tab."""
        return []


class UnknownPlugin(ValueError):
    """A `--enable-plugins`/`--disable-plugins` name that matches nothing
    installed. Fatal rather than ignored: a typo would otherwise look
    exactly like a plugin that has nothing to say."""


def split_names(values: list[str] | None) -> list[str]:
    """`["a,b", "c"]` -> `["a", "b", "c"]`, so a flag can be repeated or
    given a comma list, and the two can be mixed."""
    return [name.strip() for value in values or () for name in value.split(",") if name.strip()]


def load(group: str = GROUP) -> tuple[list, list[str]]:
    """Everything registered under `group`, plus a message per failure.

    One that raises on import is skipped rather than fatal, and that is
    load-bearing rather than merely defensive: the odooly plugin imports
    `odooly`, so `oa` installed without that extra simply has no odooly
    plugin -- the same end state as never installing it.

    Shared by both entry point groups: a manager is discovered exactly the
    way a contributor is, and neither can take the TUI down by failing.
    """
    found, failures = [], []

    for ep in entry_points(group=group):
        try:
            found.append(ep.load()())
        except Exception as exc:
            failures.append(f"{ep.name} failed to load: {exc}")

    return found, failures


def select(plugins: list[Plugin], enable: list[str], disable: list[str]) -> list[Plugin]:
    """`plugins` filtered by the two flags.

    `enable` is exclusive: with everything active by default, "also run
    odooly" would be a no-op, so naming any plugin means only those.
    `disable` subtracts from whatever that leaves, and wins on a conflict,
    which is the rule that needs no thinking about.
    """
    installed = {plugin.name for plugin in plugins}
    unknown = sorted((set(enable) | set(disable)) - installed)
    if unknown:
        known = ", ".join(sorted(installed)) or "none"
        msg = f"no such plugin: {', '.join(unknown)} (installed: {known})"
        raise UnknownPlugin(msg)

    wanted = set(enable) if enable else installed

    return [plugin for plugin in plugins if plugin.name in wanted - set(disable)]
