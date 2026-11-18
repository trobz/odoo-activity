"""The mrp plugin: read-only manufacturing data from Odoo MRP, shaped for an
agent's production skills.

This is a "scoped wrapper on a documented API" -- rung 3 of the access ladder
Anthropic's Claude for Manufacturing plugin walks in its `custom-connector`
skill: a short, curated list of read-only reads rather than raw RPC. Each
function here is the Odoo equivalent of one row of that plugin's own ERP table
(`shared/erp-connectors.md`, written for NetSuite and Infor M3): production
work orders, output, downtime, maintenance, work centers, routings, items and
sales orders.

Two rules shape every row:

* **Canonical column names.** Rows use the column names the manufacturing
  skills already recognise (`work_order`, `qty_good`, `asset_id`,
  `started_at`, ...), so rows written to CSV are read without a column
  manifest. Odoo's own name for the record travels alongside as `odoo_ref`
  (`odoo:mrp.production/42`).
* **Absent is not zero.** A field this instance's version or modules don't
  have is listed in `missing_fields` and left as None in the rows -- never
  filled with 0. Every windowed read says whether it was cut at `limit`
  (`truncated`), so a capped pull can't pass for a complete one.

Odoo stores datetimes in UTC without saying so. Every datetime here is
converted to the plant's time zone and printed with its offset
(`2026-11-13T06:00:00+07:00`), and a "day" (`plan_date`, `production_day`) is
a day in that zone. The zone is the `tz` argument, or the connecting user's
own `tz` in Odoo, or UTC with a note saying so.

Opt-in like pos, and leaning on odooly's env matching the same way: the
top-level `import odooly` means `oa` installed without the extra simply has no
mrp plugin.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import odooly

from odoo_activity.plugins import DbTarget, Plugin
from odoo_activity.plugins.odooly import match_odooly_env, read_odooly_aliases, read_odooly_envs
from odoo_activity.plugins.odooly.scripts import redact, use_user_config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from odoo_activity.plugins.odooly import OdoolyEnv

DEFAULT_LIMIT = 2000
MAX_LIMIT = 20000
# the modules whose presence decides what can be read at all
_MODULES = ("mrp", "maintenance", "mrp_maintenance", "mrp_workorder", "quality_control", "sale_mrp", "sale_stock")
_ODOO_FORMAT = "%Y-%m-%d %H:%M:%S"
_TAG = re.compile(r"<[^>]+>")


class Window(TypedDict):
    """The window a read covered: local dates, the zone they're in, and the
    UTC bounds actually sent to Odoo."""

    date_from: str
    date_to: str
    tz: str
    utc_from: str
    utc_to: str


class MrpResult(TypedDict):
    """What every read returns. `rows` is never padded: a missing field is
    named in `missing_fields`, and `truncated` says `limit` was hit."""

    env: str
    model: str
    window: Window | None
    rows: list[dict]
    truncated: bool
    missing_fields: list[str]
    notes: list[str]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _ref(model: str, rec_id: int) -> str:
    return f"odoo:{model}/{rec_id}"


def _m2o_id(value: Any) -> int | None:
    """A many2one as search_read returns it -- `[id, name]`, or False."""
    return value[0] if value else None


def _m2o_name(value: Any) -> str | None:
    return value[1] if value else None


def _text(value: Any) -> str | None:
    """Plain text from a Text or Html field (`maintenance.request.description`
    is Html from v15 on), or None when empty."""
    if not value:
        return None
    text = _TAG.sub(" ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = f"unknown time zone {name!r}"
        raise ValueError(msg) from exc


def _local(value: Any, zone: ZoneInfo) -> str | None:
    """An Odoo UTC datetime string as ISO 8601 in `zone`, with its offset."""
    if not value:
        return None
    utc = datetime.strptime(value, _ODOO_FORMAT).replace(tzinfo=timezone.utc)
    return utc.astimezone(zone).isoformat()


def _local_day(value: Any, zone: ZoneInfo) -> str | None:
    if not value:
        return None
    utc = datetime.strptime(value, _ODOO_FORMAT).replace(tzinfo=timezone.utc)
    return utc.astimezone(zone).date().isoformat()


def _elapsed_minutes(start: Any, end: Any) -> float | None:
    """Minutes between two Odoo datetimes, or None when either is unset."""
    if not start or not end:
        return None
    delta = datetime.strptime(end, _ODOO_FORMAT) - datetime.strptime(start, _ODOO_FORMAT)
    return round(delta.total_seconds() / 60, 2)


def _window(date_from: str, date_to: str, zone: ZoneInfo) -> Window:
    """Local `date_from`..`date_to` (both inclusive) as UTC bounds Odoo can
    compare against: `[utc_from, utc_to)`."""
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError as exc:
        msg = f"dates must be YYYY-MM-DD: {exc}"
        raise ValueError(msg) from exc
    if end < start:
        msg = f"date_to {date_to} is before date_from {date_from}"
        raise ValueError(msg)

    def utc(day: date) -> str:
        local = datetime(day.year, day.month, day.day, tzinfo=zone)
        return local.astimezone(timezone.utc).strftime(_ODOO_FORMAT)

    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "tz": str(zone.key),
        "utc_from": utc(start),
        "utc_to": utc(end + timedelta(days=1)),
    }


def _limit(limit: int) -> int:
    if limit < 1:
        msg = "limit must be at least 1"
        raise ValueError(msg)
    return min(limit, MAX_LIMIT)


def _fields(client: Any, model: str, wanted: Iterable[str]) -> tuple[list[str], list[str]]:
    """`wanted` split into (present on this instance, missing) -- the
    fields_get() gate every read goes through, so a version or a module
    without a field degrades to a None cell instead of a failed RPC."""
    available = client.env[model].fields_get()
    present = [f for f in wanted if f in available]
    return present, [f"{model}.{f}" for f in wanted if f not in available]


def _first(client: Any, model: str, *candidates: str) -> str | None:
    """The first of `candidates` this instance's `model` has -- for a field
    Odoo renamed between versions (`date_planned_start` -> `date_start`)."""
    available = client.env[model].fields_get()
    return next((c for c in candidates if c in available), None)


def _read_capped(client: Any, model: str, domain: list, fields: list[str], limit: int, order: str):
    """`search_read` asking for one row more than `limit`, so a capped pull
    says so instead of passing for complete (M3's own searches stop at 30
    rows and say nothing -- that's the failure this avoids)."""
    rows = client.env[model].search_read(domain, fields, limit=limit + 1, order=order)
    return rows[:limit], len(rows) > limit


def _by_id(client: Any, model: str, ids: Iterable[int | None], fields: list[str]) -> dict[int, dict]:
    """id -> row for `ids`, the empty many2ones (None) among them skipped."""
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    return {r["id"]: r for r in client.env[model].search_read([["id", "in", wanted]], fields)}


def _part_numbers(client: Any, product_ids: Iterable[int | None]) -> dict[int, str]:
    """product.product id -> part number: its internal reference
    (`default_code`), or its name when it has none."""
    products = _by_id(client, "product.product", product_ids, ["default_code", "name"])
    return {pid: p.get("default_code") or p["name"] for pid, p in products.items()}


def _asset_ids(client: Any, workcenter_ids: Iterable[int | None]) -> dict[int, str]:
    """mrp.workcenter id -> asset id: its `code`, or its name without one.
    The same key everywhere a work center appears, so downtime, output and
    routings join on it."""
    centers = _by_id(client, "mrp.workcenter", workcenter_ids, ["code", "name"])
    return {wid: w.get("code") or w["name"] for wid, w in centers.items()}


def _result(env: str, model: str, window: Window | None, rows, truncated, missing, notes) -> MrpResult:
    return {
        "env": env,
        "model": model,
        "window": window,
        "rows": rows,
        "truncated": truncated,
        "missing_fields": sorted(set(missing)),
        "notes": notes,
    }


def resolve_tz(client: Any, tz: str | None) -> tuple[ZoneInfo, list[str]]:
    """The plant's zone: `tz` if given, else the connecting user's Odoo
    `tz`, else UTC -- with a note whenever it wasn't given explicitly."""
    if tz:
        return _zone(tz), []
    [user] = client.env["res.users"].search_read([["id", "=", client.env.uid]], ["tz"])
    if user.get("tz"):
        return _zone(user["tz"]), [f"time zone {user['tz']} taken from the Odoo user; pass tz to override"]
    return _zone("UTC"), ["the Odoo user has no time zone set: days are UTC days; pass tz for the plant's own"]


# --------------------------------------------------------------------------
# the reads
# --------------------------------------------------------------------------


def mrp_scope(client: Any, env: str) -> dict:
    """The connection test: who is signed in, which company and time zone,
    the server version, and which manufacturing modules are installed --
    the Odoo counterpart of M3's `GetUserData_CurrentUser`. Pulls no
    business data."""
    [user] = client.env["res.users"].search_read([["id", "=", client.env.uid]], ["login", "tz", "company_id"])
    modules = client.env["ir.module.module"].search_read(
        [["name", "in", list(_MODULES)], ["state", "=", "installed"]], ["name"]
    )
    installed = sorted(m["name"] for m in modules)
    return {
        "env": env,
        "source_system": "odoo",
        "server_version": getattr(client, "server_version", None),
        "login": user["login"],
        "company": _m2o_name(user.get("company_id")),
        "tz": user.get("tz") or None,
        "installed": installed,
        "not_installed": sorted(set(_MODULES) - set(installed)),
    }


_MO_FIELDS = ["name", "product_id", "product_qty", "product_uom_id", "state", "date_deadline", "origin", "qty_produced"]


def _productions(client, env, date_from, date_to, tz, limit, include_closed):
    """mrp.production scheduled to start in the window -- shared by
    `mrp_work_orders` and `mrp_plan`, which present the same records two
    ways."""
    zone, notes = resolve_tz(client, tz)
    window = _window(date_from, date_to, zone)
    start_field = _first(client, "mrp.production", "date_planned_start", "date_start")
    fields, missing = _fields(client, "mrp.production", _MO_FIELDS)
    if start_field is None:
        missing.append("mrp.production.date_planned_start")

    domain: list = []
    if start_field:
        domain = [[start_field, ">=", window["utc_from"]], [start_field, "<", window["utc_to"]]]
    if not include_closed:
        domain.append(["state", "not in", ["done", "cancel"]])

    order = f"{start_field}, id" if start_field else "id"
    rows, truncated = _read_capped(
        client, "mrp.production", domain, [*fields, *filter(None, [start_field])], limit, order
    )
    parts = _part_numbers(client, (_m2o_id(r.get("product_id")) for r in rows))
    centers = _work_center_of(client, [r["id"] for r in rows])
    return zone, window, notes, start_field, rows, truncated, missing, parts, centers


def _work_center_of(client: Any, production_ids: list[int]) -> dict[int, str | None]:
    """MO id -> its asset id when all its work orders run on one work
    center, None when they span several (or it has none) -- an MO-level
    figure can only be put on a machine when the MO only used one."""
    if not production_ids:
        return {}
    orders = client.env["mrp.workorder"].search_read(
        [["production_id", "in", production_ids]], ["production_id", "workcenter_id"]
    )
    centers: dict[int, set[int | None]] = {}
    for wo in orders:
        centers.setdefault(wo["production_id"][0], set()).add(_m2o_id(wo["workcenter_id"]))
    keys = _asset_ids(client, {c for cs in centers.values() for c in cs})
    return {mo: keys.get(next(iter(cs))) if len(cs) == 1 else None for mo, cs in centers.items()}


def mrp_work_orders(
    client: Any,
    env: str,
    date_from: str,
    date_to: str,
    tz: str | None = None,
    include_closed: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> MrpResult:
    """Manufacturing orders scheduled to start in the window: the
    "production work orders" record. Open ones only unless
    `include_closed`. `qty_produced_to_date` is cumulative for the MO, not
    a day's output -- use `mrp_output` for that."""
    zone, window, notes, start_field, rows, truncated, missing, parts, centers = _productions(
        client, env, date_from, date_to, tz, _limit(limit), include_closed
    )
    out = [
        {
            "work_order": r["name"],
            "part_number": parts.get(_m2o_id(r.get("product_id"))),
            "qty_ordered": r.get("product_qty"),
            "qty_produced_to_date": r.get("qty_produced"),
            "uom": _m2o_name(r.get("product_uom_id")),
            "status": r.get("state"),
            "scheduled_start": _local(r.get(start_field), zone) if start_field else None,
            "due_date": _local(r.get("date_deadline"), zone),
            "sales_order": r.get("origin") or None,
            "asset_id": centers.get(r["id"]),
            "odoo_ref": _ref("mrp.production", r["id"]),
        }
        for r in rows
    ]
    return _result(env, "mrp.production", window, out, truncated, missing, notes)


def mrp_plan(
    client: Any, env: str, date_from: str, date_to: str, tz: str | None = None, limit: int = DEFAULT_LIMIT
) -> MrpResult:
    """The production plan as standard Odoo holds it: each MO's quantity on
    the local day it is scheduled to start, cancelled ones excluded.

    Standard Odoo has no separate plan model. A project that plans
    elsewhere (a custom plan model, an MPS) has a different plan, and this
    one is not it."""
    zone, window, notes, start_field, rows, truncated, missing, parts, centers = _productions(
        client, env, date_from, date_to, tz, _limit(limit), include_closed=True
    )
    notes = [*notes, "plan = MOs by scheduled start; a custom plan model, if the project has one, is not read"]
    out = [
        {
            "plan_date": _local_day(r.get(start_field), zone) if start_field else None,
            "part_number": parts.get(_m2o_id(r.get("product_id"))),
            "planned_qty": r.get("product_qty"),
            "uom": _m2o_name(r.get("product_uom_id")),
            "asset_id": centers.get(r["id"]),
            "work_order": r["name"],
            "odoo_ref": _ref("mrp.production", r["id"]),
        }
        for r in rows
        if r.get("state") != "cancel"
    ]
    return _result(env, "mrp.production", window, out, truncated, missing, notes)


def mrp_output(
    client: Any, env: str, date_from: str, date_to: str, tz: str | None = None, limit: int = DEFAULT_LIMIT
) -> MrpResult:
    """Good output per MO per local day, from the MO's done finished-product
    moves, with the day's done scrap for that MO beside it.

    Not `mrp.production.qty_produced`: that is cumulative, so "the MOs
    started on a day" is not "what was made that day". By-products are not
    counted as output. `qty_scrap` is in the scrap's own unit, which is
    normally the product's."""
    limit = _limit(limit)
    zone, notes = resolve_tz(client, tz)
    window = _window(date_from, date_to, zone)
    # v17 renamed the done quantity: `quantity_done` up to v16, `quantity` after
    qty_field = _first(client, "stock.move", "quantity_done", "quantity")
    missing: list[str] = [] if qty_field else ["stock.move.quantity"]
    if qty_field is None:
        return _result(env, "stock.move", window, [], False, missing, notes)

    moves, truncated = _read_capped(
        client,
        "stock.move",
        [
            ["production_id", "!=", False],
            ["state", "=", "done"],
            ["date", ">=", window["utc_from"]],
            ["date", "<", window["utc_to"]],
        ],
        ["production_id", "product_id", "product_uom", qty_field, "date"],
        limit,
        "date, id",
    )
    mo_ids = {m["production_id"][0] for m in moves}
    mos = _by_id(client, "mrp.production", mo_ids, ["name", "product_id", "origin"])

    scrap_fields, scrap_missing = _fields(client, "stock.scrap", ["production_id", "scrap_qty", "date_done"])
    missing += scrap_missing
    scrap: dict[tuple[int, str | None], float] = {}
    if len(scrap_fields) == 3:
        for s in client.env["stock.scrap"].search_read(
            [
                ["production_id", "!=", False],
                ["state", "=", "done"],
                ["date_done", ">=", window["utc_from"]],
                ["date_done", "<", window["utc_to"]],
            ],
            scrap_fields,
        ):
            key = (s["production_id"][0], _local_day(s["date_done"], zone))
            scrap[key] = scrap.get(key, 0.0) + s["scrap_qty"]
            mo_ids.add(s["production_id"][0])
        mos |= _by_id(client, "mrp.production", mo_ids - mos.keys(), ["name", "product_id", "origin"])

    totals: dict[tuple[int, str | None], dict] = {}
    for m in moves:
        mo = mos.get(m["production_id"][0])
        if mo is None or _m2o_id(m["product_id"]) != _m2o_id(mo["product_id"]):
            continue  # a by-product, not the MO's own output
        day = _local_day(m["date"], zone)
        row = totals.setdefault((mo["id"], day), {"qty": 0.0, "uom": _m2o_name(m.get("product_uom"))})
        row["qty"] += m[qty_field] or 0.0

    parts = _part_numbers(client, (_m2o_id(mo["product_id"]) for mo in mos.values()))
    centers = _work_center_of(client, sorted(mos))
    out = []
    for mo_id, day in sorted(totals.keys() | scrap.keys(), key=lambda k: (k[1], k[0])):
        mo = mos[mo_id]
        made = totals.get((mo_id, day))
        out.append({
            "production_day": day,
            "work_order": mo["name"],
            "part_number": parts.get(_m2o_id(mo["product_id"])),
            # a day with scrap but nothing finished really did finish nothing
            "qty_good": made["qty"] if made else 0.0,
            "qty_scrap": scrap.get((mo_id, day), 0.0) if len(scrap_fields) == 3 else None,
            "uom": made["uom"] if made else None,
            "asset_id": centers.get(mo_id),
            "sales_order": mo.get("origin") or None,
            "odoo_ref": _ref("mrp.production", mo_id),
        })
    return _result(env, "stock.move", window, out, truncated, missing, notes)


def mrp_downtime(
    client: Any,
    env: str,
    date_from: str,
    date_to: str,
    tz: str | None = None,
    include_productive: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> MrpResult:
    """Work-center time-tracking entries (`mrp.workcenter.productivity`)
    overlapping the window, still-open ones included (`ended_at` None).

    `loss_type` is Odoo's effectiveness category: `availability` is a stop,
    `performance` is running slow (reduced speed), `quality` is time spent
    making scrap. `productive` time is left out unless asked for, since it
    is not downtime. `reason_code` is the loss reason's name; a blank one
    is reported as None, never folded into an "Other".

    Two durations, because Odoo's own is not elapsed time:
    `duration_minutes` is `ended_at - started_at`, while `working_minutes`
    is Odoo's `duration`, which for availability and quality losses counts
    only the minutes inside the work center's working calendar (a stop at
    night on a day-shift calendar is 0 there). A large gap between the two
    is worth a look at that calendar -- its zone above all."""
    limit = _limit(limit)
    zone, notes = resolve_tz(client, tz)
    window = _window(date_from, date_to, zone)
    model = "mrp.workcenter.productivity"
    wanted = ["workcenter_id", "loss_id", "date_start", "date_end", "duration", "description", "workorder_id"]
    fields, missing = _fields(client, model, wanted)

    rows, truncated = _read_capped(
        client,
        model,
        [
            ["date_start", "<", window["utc_to"]],
            "|",
            ["date_end", "=", False],
            ["date_end", ">=", window["utc_from"]],
        ],
        fields,
        limit,
        "date_start, id",
    )
    losses = _by_id(
        client, "mrp.workcenter.productivity.loss", (_m2o_id(r.get("loss_id")) for r in rows), ["name", "loss_type"]
    )
    centers = _asset_ids(client, (_m2o_id(r.get("workcenter_id")) for r in rows))
    wo_ids = {_m2o_id(r.get("workorder_id")) for r in rows} - {None}
    workorders = _by_id(client, "mrp.workorder", wo_ids, ["production_id"])

    out = []
    for r in rows:
        loss = losses.get(_m2o_id(r.get("loss_id")))
        loss_type = loss.get("loss_type") if loss else None
        if loss_type == "productive" and not include_productive:
            continue
        wo = workorders.get(_m2o_id(r.get("workorder_id")))
        out.append({
            "asset_id": centers.get(_m2o_id(r.get("workcenter_id"))),
            "started_at": _local(r.get("date_start"), zone),
            "ended_at": _local(r.get("date_end"), zone),
            # None (not 0) while still open
            "duration_minutes": _elapsed_minutes(r.get("date_start"), r.get("date_end")),
            "working_minutes": r.get("duration") if r.get("date_end") else None,
            "reason_code": loss["name"] if loss else None,
            "loss_type": loss_type,
            "reason_text": _text(r.get("description")),
            "work_order": _m2o_name(wo.get("production_id")) if wo else None,
            "odoo_ref": _ref(model, r["id"]),
        })
    return _result(env, model, window, out, truncated, missing, notes)


def mrp_maintenance(
    client: Any, env: str, date_from: str, date_to: str, tz: str | None = None, limit: int = DEFAULT_LIMIT
) -> MrpResult:
    """Maintenance requests raised in the window, plus every request still
    open whenever it was raised -- an open repair from last month matters
    to today's report.

    `asset_id` is the equipment's name. It joins to a work center's
    `asset_id` only when they are named alike, or when mrp_maintenance
    links the equipment to a work center (`work_center`, None otherwise)."""
    limit = _limit(limit)
    zone, notes = resolve_tz(client, tz)
    window = _window(date_from, date_to, zone)
    model = "maintenance.request"
    wanted = ["name", "equipment_id", "maintenance_type", "request_date", "close_date", "description", "stage_id"]
    fields, missing = _fields(client, model, wanted)

    done_stages = [s["id"] for s in client.env["maintenance.stage"].search_read([["done", "=", True]], ["id"])]
    rows, truncated = _read_capped(
        client,
        model,
        [
            ["request_date", "<=", window["date_to"]],
            "|",
            ["request_date", ">=", window["date_from"]],
            ["stage_id", "not in", done_stages],
        ],
        fields,
        limit,
        "request_date, id",
    )
    eq_fields, _ = _fields(client, "maintenance.equipment", ["name", "workcenter_id"])
    equipment = _by_id(client, "maintenance.equipment", (_m2o_id(r.get("equipment_id")) for r in rows), eq_fields)
    centers = _asset_ids(client, (_m2o_id(e.get("workcenter_id")) for e in equipment.values()))

    out = []
    for r in rows:
        eq = equipment.get(_m2o_id(r.get("equipment_id")))
        out.append({
            "maintenance_wo": r.get("name"),
            "asset_id": eq["name"] if eq else None,
            "work_center": centers.get(_m2o_id(eq.get("workcenter_id"))) if eq else None,
            "type": r.get("maintenance_type"),
            # request_date/close_date are plain dates in Odoo -- no zone to convert
            "opened_date": r.get("request_date") or None,
            "closed_date": r.get("close_date") or None,
            "is_open": _m2o_id(r.get("stage_id")) not in done_stages,
            "stage": _m2o_name(r.get("stage_id")),
            "close_out_note": _text(r.get("description")),
            "odoo_ref": _ref(model, r["id"]),
        })
    return _result(env, model, window, out, truncated, missing, notes)


def _capacity_rows(client: Any, workcenter_ids: Iterable[int | None]) -> dict[int, list[dict]] | None:
    """work center id -> its `mrp.workcenter.capacity` rows, or None on a
    version without them (v16 and earlier keep one `capacity` on the work
    center itself). Each row: product_id, product_tmpl_id, capacity,
    time_start, time_stop."""
    if "capacity_ids" not in client.env["mrp.workcenter"].fields_get():
        return None
    ids = sorted({i for i in workcenter_ids if i})
    rows = (
        client.env["mrp.workcenter.capacity"].search_read(
            [["workcenter_id", "in", ids]], ["workcenter_id", "product_id", "capacity", "time_start", "time_stop"]
        )
        if ids
        else []
    )
    templates = _by_id(client, "product.product", (_m2o_id(r.get("product_id")) for r in rows), ["product_tmpl_id"])
    by_center: dict[int, list[dict]] = {}
    for r in rows:
        product = templates.get(_m2o_id(r.get("product_id")))
        r["product_tmpl_id"] = _m2o_id(product["product_tmpl_id"]) if product else None
        by_center.setdefault(r["workcenter_id"][0], []).append(r)
    return by_center


def _operation_capacity(bom: dict, wc: dict, rows: list[dict] | None) -> tuple[float | None, float | None]:
    """(capacity, setup + cleanup minutes) for one BoM operation, the way
    Odoo sizes it. Up to v16: the work center's own `capacity` and times.
    From v17: a capacity row for this product, else one for any product,
    else the BoM quantity with the work center's times -- `_get_capacity`'s
    order, without its unit-of-measure matching."""
    wc_setup = (wc.get("time_start") or 0.0) + (wc.get("time_stop") or 0.0) if wc else None
    if rows is None:
        return wc.get("capacity"), wc_setup

    variant, template = _m2o_id(bom.get("product_id")), _m2o_id(bom.get("product_tmpl_id"))
    specific = [
        r
        for r in rows
        if r.get("product_id")
        and r["product_tmpl_id"] == template
        and (variant is None or _m2o_id(r["product_id"]) == variant)
    ]
    generic = [r for r in rows if not r.get("product_id")]
    match = (specific or generic or [None])[0]
    bom_qty = bom.get("product_qty") or 1.0
    if match is None:
        return bom_qty, wc_setup
    return match.get("capacity") or bom_qty, (match.get("time_start") or 0.0) + (match.get("time_stop") or 0.0)


_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def mrp_work_centers(client: Any, env: str) -> MrpResult:
    """Every active work center, with what OEE and costing need: capacity,
    efficiency, OEE target, cost per hour, setup/cleanup minutes, and its
    working-time calendar as attendance slots (local hours in the
    calendar's own zone) -- the planned production time availability is
    measured against. Public holidays and leaves are not included."""
    model = "mrp.workcenter"
    wanted = [
        "name",
        "code",
        "time_efficiency",
        "oee_target",
        "costs_hour",
        "time_start",
        "time_stop",
        "resource_calendar_id",
    ]
    fields, missing = _fields(client, model, wanted)
    has_capacity = "capacity" in client.env[model].fields_get()
    rows = client.env[model].search_read([], [*fields, *(["capacity"] if has_capacity else [])], order="name, id")
    per_product = _capacity_rows(client, (r["id"] for r in rows))
    notes = []
    if per_product is not None:
        notes.append(
            "capacity is per product on this version (product_capacities); "
            "without a matching row Odoo uses the BoM quantity"
        )
    elif not has_capacity:
        missing.append(f"{model}.capacity")
    parts = _part_numbers(client, (_m2o_id(c.get("product_id")) for cs in (per_product or {}).values() for c in cs))
    calendars = _by_id(
        client, "resource.calendar", (_m2o_id(r.get("resource_calendar_id")) for r in rows), ["name", "tz"]
    )
    att_fields, att_missing = _fields(
        client,
        "resource.calendar.attendance",
        ["calendar_id", "dayofweek", "hour_from", "hour_to", "week_type", "day_period"],
    )
    missing += att_missing
    slots: dict[int, list[dict]] = {}
    if calendars:
        for a in client.env["resource.calendar.attendance"].search_read(
            [["calendar_id", "in", sorted(calendars)]], att_fields, order="dayofweek, hour_from"
        ):
            if a.get("day_period") == "lunch":
                continue  # v17+ stores the lunch break as its own slot
            slots.setdefault(a["calendar_id"][0], []).append({
                "day": _DAYS[int(a["dayofweek"])],
                "from": a["hour_from"],
                "to": a["hour_to"],
                "week_type": a.get("week_type") or None,
            })

    out = []
    for r in rows:
        cal_id = _m2o_id(r.get("resource_calendar_id"))
        cal = calendars.get(cal_id)
        out.append({
            "asset_id": r.get("code") or r["name"],
            "asset_name": r["name"],
            "capacity": r.get("capacity") if has_capacity else None,
            "product_capacities": [
                {
                    "part_number": parts.get(_m2o_id(c.get("product_id"))),  # None: any product
                    "capacity": c.get("capacity"),
                    "setup_minutes": c.get("time_start"),
                    "cleanup_minutes": c.get("time_stop"),
                }
                for c in per_product.get(r["id"], [])
            ]
            if per_product is not None
            else None,
            "time_efficiency_pct": r.get("time_efficiency"),
            "oee_target_pct": r.get("oee_target"),
            "costs_hour": r.get("costs_hour"),
            "setup_minutes": r.get("time_start"),
            "cleanup_minutes": r.get("time_stop"),
            "calendar": cal["name"] if cal else None,
            "calendar_tz": cal.get("tz") if cal else None,
            "attendance": slots.get(cal_id, []) if cal else None,
            "odoo_ref": _ref(model, r["id"]),
        })
    return _result(env, model, None, out, False, missing, notes)


def mrp_routings(client: Any, env: str, part_numbers: list[str] | None = None, limit: int = DEFAULT_LIMIT) -> MrpResult:
    """Operations of each active BoM, for the given part numbers or all of
    them: the ideal cycle time OEE performance and quoting need.

    `run_min_per_unit` is the operation's cycle time divided by its
    capacity, which is how Odoo itself sizes a work order's expected
    duration, before efficiency. The capacity is the work center's (v16 and
    earlier) or the matching per-product capacity, else the BoM quantity
    (v17+). v14+ only: before that,
    operations hang off `mrp.routing` rather than the BoM."""
    limit = _limit(limit)
    model = "mrp.routing.workcenter"
    if "bom_id" not in client.env[model].fields_get():
        return _result(
            env, model, None, [], False, [f"{model}.bom_id"], ["routings are read on Odoo 14 and later only"]
        )

    bom_domain: list = []
    notes: list[str] = []
    if part_numbers:
        products = client.env["product.product"].search_read(
            [["default_code", "in", part_numbers]], ["product_tmpl_id", "default_code"]
        )
        found = {p["default_code"] for p in products}
        if set(part_numbers) - found:
            notes.append(f"no product with internal reference: {', '.join(sorted(set(part_numbers) - found))}")
        bom_domain = [["product_tmpl_id", "in", sorted({p["product_tmpl_id"][0] for p in products})]]
    boms = client.env["mrp.bom"].search_read(bom_domain, ["product_tmpl_id", "product_id", "product_qty", "code"])
    if not boms:
        return _result(env, model, None, [], False, [], notes)

    wanted = ["bom_id", "name", "sequence", "workcenter_id", "time_mode", "time_cycle_manual", "time_cycle"]
    fields, missing = _fields(client, model, wanted)
    ops, truncated = _read_capped(
        client, model, [["bom_id", "in", [b["id"] for b in boms]]], fields, limit, "bom_id, sequence, id"
    )

    wc_fields, _ = _fields(client, "mrp.workcenter", ["code", "name", "capacity", "time_start", "time_stop"])
    centers = _by_id(client, "mrp.workcenter", (_m2o_id(o.get("workcenter_id")) for o in ops), wc_fields)
    capacities = _capacity_rows(client, centers)
    templates = _by_id(
        client, "product.template", (_m2o_id(b["product_tmpl_id"]) for b in boms), ["default_code", "name"]
    )
    variants = _part_numbers(client, (_m2o_id(b.get("product_id")) for b in boms))
    bom_by_id = {b["id"]: b for b in boms}

    out = []
    for o in ops:
        bom = bom_by_id[o["bom_id"][0]]
        tmpl = templates.get(_m2o_id(bom["product_tmpl_id"]), {})
        wc = centers.get(_m2o_id(o.get("workcenter_id")), {})
        cycle = o.get("time_cycle") or o.get("time_cycle_manual")
        rows = None if capacities is None else capacities.get(_m2o_id(o.get("workcenter_id")), [])
        cap, setup = _operation_capacity(bom, wc, rows)
        out.append({
            "part_number": variants.get(_m2o_id(bom.get("product_id"))) or tmpl.get("default_code") or tmpl.get("name"),
            "op_seq": o.get("sequence"),
            "operation": o.get("name"),
            "work_center": wc.get("code") or wc.get("name"),
            "time_mode": o.get("time_mode"),
            "cycle_minutes": cycle,
            "capacity": cap,
            "run_min_per_unit": cycle / cap if cycle and cap else None,
            "setup_hours": setup / 60 if setup is not None else None,
            "bom": bom.get("code") or None,
            "odoo_ref": _ref(model, o["id"]),
        })
    return _result(env, model, None, out, truncated, missing, notes)


def mrp_items(client: Any, env: str, part_numbers: list[str]) -> MrpResult:
    """The part master for the given part numbers: name, unit, standard
    cost, and whether Odoo would make or buy it (a BoM vs. a vendor). The
    cost is the connecting user's company's -- `standard_price` is per
    company -- and None, never 0, when it is unset."""
    if not part_numbers:
        msg = "mrp_items needs part_numbers -- the part master is never pulled whole"
        raise ValueError(msg)
    model = "product.product"
    fields, missing = _fields(
        client, model, ["default_code", "name", "product_tmpl_id", "uom_id", "standard_price", "seller_ids"]
    )
    rows = client.env[model].search_read([["default_code", "in", part_numbers]], fields)
    notes = []
    absent = set(part_numbers) - {r["default_code"] for r in rows}
    if absent:
        notes.append(f"no product with internal reference: {', '.join(sorted(absent))}")

    # a BoM making this variant, or its template with no variant pinned --
    # not `bom_count`, which also counts BoMs where it is only a by-product
    made_variants: set[int] = set()
    made_templates: set[int] = set()
    if rows:
        for bom in client.env["mrp.bom"].search_read(
            [
                "|",
                ["product_id", "in", [r["id"] for r in rows]],
                ["product_tmpl_id", "in", sorted({_m2o_id(r.get("product_tmpl_id")) for r in rows} - {None})],
            ],
            ["product_id", "product_tmpl_id"],
        ):
            if bom.get("product_id"):
                made_variants.add(bom["product_id"][0])
            else:
                made_templates.add(bom["product_tmpl_id"][0])

    out = []
    for r in rows:
        make = r["id"] in made_variants or _m2o_id(r.get("product_tmpl_id")) in made_templates
        buy = bool(r.get("seller_ids"))
        out.append({
            "part_number": r["default_code"],
            "description": r["name"],
            "uom": _m2o_name(r.get("uom_id")),
            "std_cost": r.get("standard_price") or None,
            "make_buy": "make" if make and not buy else "buy" if buy and not make else "both" if make else None,
            "odoo_ref": _ref(model, r["id"]),
        })
    return _result(env, model, None, out, False, missing, notes)


def mrp_sales_orders(client: Any, env: str, names: list[str]) -> MrpResult:
    """The sales order lines behind the given orders (an MO's `sales_order`
    is its `origin`): customer, part, quantity, and the promised date --
    the commitment date if one was set, else the expected date. Lets a
    late MO be traced to the customer promise it puts at risk."""
    if not names:
        msg = "mrp_sales_orders needs the order names, e.g. the sales_order of a late work order"
        raise ValueError(msg)
    model = "sale.order"
    fields, missing = _fields(
        client, model, ["name", "partner_id", "date_order", "commitment_date", "expected_date", "state"]
    )
    orders = {o["id"]: o for o in client.env[model].search_read([["name", "in", names]], fields)}
    notes = []
    absent = set(names) - {o["name"] for o in orders.values()}
    if absent:
        notes.append(f"no sales order named: {', '.join(sorted(absent))}")
    zone, tz_notes = resolve_tz(client, None)
    lines = (
        client.env["sale.order.line"].search_read(
            [["order_id", "in", sorted(orders)], ["display_type", "=", False]],
            ["order_id", "product_id", "product_uom_qty", "qty_delivered"],
        )
        if orders
        else []
    )
    parts = _part_numbers(client, (_m2o_id(ln.get("product_id")) for ln in lines))

    out = []
    for ln in lines:
        so = orders[ln["order_id"][0]]
        out.append({
            "sales_order": so["name"],
            "customer": _m2o_name(so.get("partner_id")),
            "part_number": parts.get(_m2o_id(ln.get("product_id"))),
            "qty_ordered": ln.get("product_uom_qty"),
            "qty_delivered": ln.get("qty_delivered"),
            "order_date": _local(so.get("date_order"), zone),
            "promised_date": _local(so.get("commitment_date") or so.get("expected_date"), zone),
            "status": so.get("state"),
            "odoo_ref": _ref("sale.order.line", ln["id"]),
        })
    return _result(env, model, None, out, False, missing, [*notes, *tz_notes])


# --------------------------------------------------------------------------
# connecting
# --------------------------------------------------------------------------


def connect(env: str) -> tuple[Any, str]:
    """An odooly client for `env`, or a message saying why not -- the
    password is masked out of any error."""
    use_user_config()
    password = ""
    try:
        password = odooly.read_config(env)[3] or ""
        return odooly.Client.from_config(env), ""
    except Exception as exc:  # a missing section, a server that won't answer, ...
        return None, f"cannot connect to '{env}': {redact(str(exc), password)}"


def run(env: str, read: Callable[..., Any], **kwargs: Any) -> tuple[Any, str]:
    """Connect to `env` and run one of the reads above on it, as
    `(result, "")` or `(None, message)` -- the shape oa-mcp's tools unwrap.

    An Odoo error (a model that isn't installed, an access right the user
    lacks) comes back as the message rather than a traceback: "maintenance
    is not installed" is an answer, not a crash."""
    client, message = connect(env)
    if client is None:
        return None, message
    try:
        return read(client, env, **kwargs), ""
    except ValueError:
        raise  # a bad argument: the caller's to fix, said as-is
    except Exception as exc:
        return None, f"{getattr(read, '__name__', 'read')} on '{env}' failed: {exc}"


class MrpPlugin(Plugin):
    """mrp's TUI contribution: a database-mode MRP tab listing the open
    manufacturing orders scheduled a week either side of today. The reads
    themselves are for oa-mcp; see mcp_server.py."""

    name = "mrp"
    default = False  # opt-in -- most projects don't run manufacturing
    requires = ("odooly",)

    def __init__(self) -> None:
        self.envs: list[OdoolyEnv] = read_odooly_envs()
        self.aliases = read_odooly_aliases()

    def env_for(self, target: DbTarget) -> str | None:
        inst, db = target
        return match_odooly_env(inst["name"], db, self.envs, self.aliases) if self.envs else None

    def db_tab(self) -> str:
        return "MRP"

    def fetch_tab(self, tab: str, target: DbTarget) -> tuple[list[dict] | None, str]:
        env = self.env_for(target)
        if env is None:
            return None, "(no odooly env for this database)"

        today = date.today()
        result, message = run(
            env,
            mrp_work_orders,
            date_from=(today - timedelta(days=7)).isoformat(),
            date_to=(today + timedelta(days=7)).isoformat(),
            limit=500,
        )
        if result is None:
            return None, message
        if not result["rows"]:
            return None, "(no open manufacturing orders scheduled within a week of today)"
        return [{k: v for k, v in row.items() if k != "odoo_ref"} for row in result["rows"]], ""
