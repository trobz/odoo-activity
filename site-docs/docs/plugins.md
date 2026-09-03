---
icon: lucide/puzzle
description: Optional features ship as plugins — install the extra to get them, and add your project's own scripts without packaging anything.
tags:
  - plugins
  - odooly
---

# Plugins

Most of what `oa` shows is read-only and always there. Anything that needs
credentials, or that only some people want, ships as a plugin instead — so
it is installed rather than switched on, and absent installs cost nothing.

## Installing

```
uv tool install "odoo-activity[odooly]"     # one
uv tool install "odoo-activity[all]"        # every bundled plugin
```

Installed is active: installing was the deliberate act, so there is no
second opt-in to remember. Two flags narrow it for a single run:

```
oa --enable-plugins=odooly        # only these
oa --disable-plugins=nginx        # everything except these
```

Both take a comma list or can be repeated. If the two disagree, disabling
wins. A name matching nothing installed stops `oa` with an error rather
than running on quietly — a typo should not look like a plugin that has
nothing to say.

## odooly

The one bundled plugin today. It reads `~/odooly.ini` at startup and matches
each database against it, which unlocks the actions that need a login:

- an `ODOOLY` tag on every database row — green where an environment reaches
  it, red where none does
- **Database ▸ Toolbox** — copy the `odooly` command for this database,
  restore app icons
- **Create test job** under Jobs, **Send test mail** under Mail

An action appears only when its plugin is active *and* has something to
offer for the highlighted row. Without a matching `~/odooly.ini` section the
Toolbox says so rather than listing tools that would only fail.

!!! note "Which Toolbox"

    Instance mode and database mode both have a Toolbox tab. odooly's rows
    are on the **database** one — highlight the `└── dbname` row, not the
    instance above it.

odooly always runs on *your* machine, even when you are watching a remote
host: it reaches the instance over the network using your own
`~/odooly.ini`, which the watched host neither has nor should be asked for.

### Your project's own scripts

Some scripts only make sense for one customer project. Rather than packaging
one plugin per project, odooly's list is open: launch `oa` from a project
checkout and the Python files in its `scripts/` directory are offered
alongside the packaged ones, for the databases that project's odooly env
matches.

```
~/code/demeter
├── scripts/
│   └── recompute_zones.py     →  "recompute zones (project)" in the Toolbox
└── ...
```

Nothing to install and nothing to register — the scripts live in the
project's own repository. Files starting with `_` are skipped, so shared
helpers can sit next to the scripts that use them. They run under the same
interpreter as `oa`, so they can import the packaged helpers:

```python
from odoo_activity.plugins.odooly.scripts import redact, use_user_config
```

It has to be the directory you started `oa` from, not the instance's
directory on the server: odooly runs locally, so a script on the far end is
not somewhere it can reach.

## Writing one

Plugins are found through Python entry points, so a plugin is a package that
registers itself. There are two kinds, and they ask different things of the
app.

### Contributors

A contributor adds to an instance the app already found. Every hook is
optional — write only the ones you use.

```python
from odoo_activity.plugins import Plugin

class DemeterPlugin(Plugin):
    name = "demeter"

    def marker(self, target):        # a tag on the database row
    def tools(self, mode, target):   # Toolbox rows
    def actions(self, tab, target):  # buttons under a database tab
    def hint(self, mode, target):    # why the Toolbox is empty, if it is
```

```toml
[project.entry-points."odoo_activity.plugins"]
demeter = "oa_demeter:DemeterPlugin"
```

`target` is the highlighted `(instance, database)` pair. A handler receives
the app — so it can confirm, prompt or notify — and returns the text to show
in the pane body, or `None` if it has already said its piece.

### Managers

A manager decides what an instance *is* and how to reach it: how to find
them, how to run a command against one, where its log comes from, how to
read its config, how to connect to its databases, what start and stop mean.

```python
from odoo_activity.managers.base import Manager

class PodmanManager(Manager):
    name = "podman"
    order = 45          # where its instances sit in the list

    def instances(self, host): ...
    def host_for(self, inst, host): ...
```

```toml
[project.entry-points."odoo_activity.managers"]
podman = "oa_podman:PodmanManager"
```

`host_for` is the one that matters. Return a `Host` that already routes to
where the instance lives — the docker manager returns one that prefixes
`docker exec` and reports `is_local = False` — and every shared probe works
against it unchanged. Override the rest only where that is not enough.

!!! warning "Entry points come from installed metadata"

    Not from the source tree. After adding or changing an entry point,
    reinstall (`uv sync`, or `uv tool install --force --editable .`) —
    otherwise the new code runs while discovering nothing at all.

A plugin that fails to import is skipped with a message rather than taking
the TUI down. That is load-bearing, not merely defensive: the odooly plugin
imports `odooly`, so `oa` installed without that extra simply has no odooly
plugin.
