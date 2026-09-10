"""The pos plugin: a database-mode "POS" tab reporting `pos.config` session
status and hardware-proxy settings.

Opt-in (`--enable-plugins=pos`), unlike odooly -- most projects don't run
Point of Sale, so this shouldn't show up uninvited on ones that do nothing
with it. It leans entirely on odooly's own env-matching (`env_for` below is
the same shape as `OdoolyPlugin.env_for`) rather than duplicating it. Unlike
odooly's own plugin, the top-level `import odooly` here isn't just an
availability check -- `fetch_tab` uses it directly -- but it still buys the
same thing: without the extra, `load()` skips this plugin at import time
rather than it loading half-broken.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import odooly

from odoo_activity.plugins import DbTarget, Plugin
from odoo_activity.plugins.odooly import match_odooly_env, read_odooly_envs
from odoo_activity.plugins.odooly.scripts import redact, use_user_config

if TYPE_CHECKING:
    from odoo_activity.plugins.odooly import OdoolyEnv

# pos.session states other than these count as "open" -- 'opening_control'
# and 'closing_control' are still a till in someone's hands, not a config
# sitting idle
_CLOSED_STATES = ("closed",)

# the hardware-proxy fields worth a glance alongside session status. Not
# every version of pos.config carries all of these -- `other_devices` is
# absent before v15, for instance, while `is_posbox`/`proxy_ip`/the
# `iface_*` booleans go back at least to v12 -- so each one is checked
# against fields_get() before being asked for, and a row simply leaves the
# cell blank (see probes.stringify) on a version missing one rather than
# raising a KeyError.
_PROXY_FIELDS = [
    "is_posbox",  # the "IoT Box" checkbox gating the section below it in the UI
    "proxy_ip",
    "other_devices",
    "iface_cashdrawer",
    "iface_electronic_scale",
    "iface_print_via_proxy",
    "iface_scan_via_proxy",
]

# "wait for the terminal's own confirmation before moving on" -- a terminal
# silently left not waiting for its return is the kind of thing this tab
# exists to catch. OCA's pos_payment_terminal_return module has shipped this
# under two different homes depending on when POS grew a real
# pos.payment.method model (v14): before that it's one flag on pos.config
# itself (checked against `available` below, like the proxy fields); from
# v14 on it's per payment method, `oca_payment_terminal_return` on
# pos.payment.method (checked in `_payment_methods`). Either way, absent on
# an instance without the module -- same fields_get() gate as the rest.
_CONFIG_WAIT_FIELD = "iface_payment_terminal_return"
_METHOD_WAIT_FIELD = "oca_payment_terminal_return"


def pos_status(client: odooly.Client) -> list[dict]:
    """One row per `pos.config`, sorted by name: session status, order
    count, when its latest session opened and its latest order was rung up,
    and the hardware-proxy/payment-terminal settings above."""
    config_model = client.env["pos.config"]
    available = set(config_model.fields_get())
    proxy_fields = [f for f in _PROXY_FIELDS if f in available]
    # `pos.payment.method` (and this field pointing at it) is a v14+ model --
    # v12/v13 configure payment through plain `journal_ids` (account.journal)
    # instead, so `payment_methods` is left blank there rather than trying
    # (and failing) to reach a model that doesn't exist. The wait-for-return
    # flag, unlike the methods themselves, has an older home to fall back to
    # (see `_CONFIG_WAIT_FIELD` above) so it isn't lost on those versions.
    has_payment_methods = "payment_method_ids" in available
    has_config_wait_field = _CONFIG_WAIT_FIELD in available
    has_order_config_id = "config_id" in client.env["pos.order"].fields_get()

    fields = [
        "name",
        *proxy_fields,
        *(["payment_method_ids"] if has_payment_methods else []),
        *([_CONFIG_WAIT_FIELD] if has_config_wait_field else []),
    ]
    configs = config_model.search_read([], fields)
    methods_by_config = _payment_methods(client, configs) if has_payment_methods else {}
    latest_session_by_config = _latest_sessions(client, configs)
    order_counts_by_session = _order_counts_by_session(client, latest_session_by_config.values())
    latest_order_by_config = _latest_order_dates(client, configs) if has_order_config_id else {}

    rows = []
    for config in sorted(configs, key=lambda c: c["name"]):
        session = latest_session_by_config.get(config["id"])
        is_open = session is not None and session["state"] not in _CLOSED_STATES
        orders = order_counts_by_session.get(session["id"]) if session else None
        methods = methods_by_config.get(config["id"], [])
        latest_order = latest_order_by_config.get(config["id"])

        if has_payment_methods:
            # per-method granularity, v14+
            wait = ", ".join(m["name"] for m in methods if m.get(_METHOD_WAIT_FIELD)) or None
        else:
            # one flag for the whole till, pre-v14 -- there's no method to name
            wait = config.get(_CONFIG_WAIT_FIELD) if has_config_wait_field else None

        rows.append({
            "name": config["name"],
            "status": "open" if is_open else "closed",
            "orders": orders,
            "latest_session": session["name"] if session else None,
            "open_time": session["start_at"] if session else None,
            "latest_order": latest_order,
            "payment_methods": ", ".join(m["name"] for m in methods) or None,
            "payment_terminal_wait": wait,
            "iot_box": config.get("is_posbox"),
            "proxy_ip": config.get("proxy_ip"),
            "cashdrawer": config.get("iface_cashdrawer"),
            "electronic_scale": config.get("iface_electronic_scale"),
            "print_via_proxy": config.get("iface_print_via_proxy"),
            "scan_via_proxy": config.get("iface_scan_via_proxy"),
            "other_devices": config.get("other_devices"),
        })

    return rows


def _latest_sessions(client: odooly.Client, configs: list[dict]) -> dict[int, dict]:
    """config id -> its most recent `pos.session` row (`id`, `name`, `state`,
    `start_at`) -- one query across every config rather than one per row,
    relying on `start_at desc` so the first row seen per config is its
    latest."""
    config_ids = [config["id"] for config in configs]
    if not config_ids:
        return {}

    sessions = client.env["pos.session"].search_read(
        [["config_id", "in", config_ids]],
        ["config_id", "name", "state", "start_at"],
        order="start_at desc",
    )
    latest: dict[int, dict] = {}
    for session in sessions:
        config_id = session["config_id"][0]
        latest.setdefault(config_id, session)
    return latest


def _order_counts_by_session(client: odooly.Client, sessions: Iterable[dict]) -> dict[int, int]:
    """session id -> its `pos.order` count, for the given sessions -- one
    query across every session rather than one `search_count` per row."""
    session_ids = [session["id"] for session in sessions]
    if not session_ids:
        return {}

    orders = client.env["pos.order"].search_read([["session_id", "in", session_ids]], ["session_id"])
    counts: dict[int, int] = {}
    for order in orders:
        session_id = order["session_id"][0]
        counts[session_id] = counts.get(session_id, 0) + 1
    return counts


def _latest_order_dates(client: odooly.Client, configs: list[dict]) -> dict[int, str]:
    """config id -> the `date_order` of its most recent `pos.order`, across
    every session -- a session can open and close with nothing rung up on
    it, which `open_time` alone wouldn't say.

    A `read_group` aggregate (`date_order:max`), not `search_read` -- `pos.order`
    can hold years of history (a real one seen with 1.28M rows), and
    `search_read` would fetch one row per order just to keep the first
    per config; `read_group` computes the max in the database and returns
    one row per config regardless of how many orders it has.
    """
    config_ids = [config["id"] for config in configs]
    if not config_ids:
        return {}

    groups = client.env["pos.order"].read_group(
        [["config_id", "in", config_ids]], ["config_id", "date_order:max"], ["config_id"]
    )
    return {group["config_id"][0]: group["date_order"] for group in groups if group["config_id"]}


def _payment_methods(client: odooly.Client, configs: list[dict]) -> dict[int, list[dict]]:
    """config id -> its payment method rows (`id`, `name`, and
    `_METHOD_WAIT_FIELD` when that field is installed) -- fetched once for
    every config here rather than per row."""
    method_model = client.env["pos.payment.method"]
    method_ids = sorted({mid for config in configs for mid in config.get("payment_method_ids", [])})
    if not method_ids:
        return {}

    has_wait_field = _METHOD_WAIT_FIELD in method_model.fields_get()
    fields = ["name", *([_METHOD_WAIT_FIELD] if has_wait_field else [])]
    methods = {m["id"]: m for m in method_model.search_read([["id", "in", method_ids]], fields)}

    return {
        config["id"]: [methods[mid] for mid in config.get("payment_method_ids", []) if mid in methods]
        for config in configs
    }


def fetch_pos_status(env: str) -> tuple[list[dict] | None, str]:
    """Connect to odooly env `env` and return its `pos_status` rows, or a
    message explaining why not -- the same `(rows, message)` shape
    `PosPlugin.fetch_tab` hands the TUI, reused directly by oa-mcp's
    `pos_status` tool (see mcp_server.py) the way `run_odooly_script` is
    shared between `OdoolyPlugin` and `odooly_run_script`.
    """
    use_user_config()
    password = ""
    try:
        password = odooly.read_config(env)[3] or ""
        client = odooly.Client.from_config(env)
    except Exception as exc:  # a missing section, a server that won't answer, ...
        return None, f"cannot connect to '{env}': {redact(str(exc), password)}"

    return pos_status(client), ""


class PosPlugin(Plugin):
    """pos's one contribution: the database-mode POS tab."""

    name = "pos"
    default = False  # opt-in -- most projects don't run Point of Sale
    # so its ODOOLY marker/Toolbox still show even when only pos is named
    requires = ("odooly",)

    def __init__(self) -> None:
        self.envs: list[OdoolyEnv] = read_odooly_envs()

    def env_for(self, target: DbTarget) -> str | None:
        """The env serving this database, or None when none matches --
        same matching odooly's own plugin uses (see `OdoolyPlugin.env_for`)."""
        inst, db = target
        return match_odooly_env(inst["name"], db, self.envs) if self.envs else None

    def db_tab(self) -> str:
        return "POS"

    def fetch_tab(self, tab: str, target: DbTarget) -> tuple[list[dict] | None, str]:
        env = self.env_for(target)
        if env is None:
            return None, "(no odooly env for this database)"

        return fetch_pos_status(env)
