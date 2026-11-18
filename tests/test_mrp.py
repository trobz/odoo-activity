"""The mrp plugin's reads, against a fake Odoo.

Like test_pos.py, what matters is which records each read asks for and how
it turns them into rows -- not the RPC. The fake evaluates the domains the
reads send (prefix `|`, `=`, `in`, `<`, ...) over in-memory rows, so a read
asking for the wrong window or state gets the wrong rows here too.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from odoo_activity import mcp_server
from odoo_activity.plugins import mrp

HCM = "Asia/Ho_Chi_Minh"  # UTC+7, no DST


def _value(v):
    """A many2one compares by id, as Odoo's domain does."""
    return v[0] if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int) else v


def _leaf(row, field, op, value):
    v = _value(row.get(field, False))
    if op == "=":
        return (not v) if value is False else v == value
    if op == "!=":
        return bool(v) if value is False else v != value
    if op == "in":
        return v in value
    if op == "not in":
        return v not in value
    if v in (False, None):
        return False
    return {"<": v < value, "<=": v <= value, ">": v > value, ">=": v >= value}[op]


def _match(row, domain):
    def walk(i):
        term = domain[i]
        if term == "|":
            a, i = walk(i + 1)
            b, i = walk(i)
            return a or b, i
        if term == "&":
            a, i = walk(i + 1)
            b, i = walk(i)
            return a and b, i
        return _leaf(row, *term), i + 1

    i, ok = 0, True
    while i < len(domain):
        res, i = walk(i)
        ok = ok and res
    return ok


class FakeModel:
    """`rows` are records (each with an `id`); `fields` is what fields_get()
    reports -- leave one out to simulate a version or module without it."""

    def __init__(self, rows=(), fields=None):
        self.rows = list(rows)
        self.fields = set(fields) if fields is not None else {k for r in self.rows for k in r} - {"id"}
        self.calls = []

    def fields_get(self):
        return {f: {} for f in self.fields}

    def search_read(self, domain, fields, limit=None, order=None):
        self.calls.append(domain)
        rows = [r for r in self.rows if _match(r, domain)]
        if order:
            key = order.split(",")[0].split()[0]
            rows.sort(key=lambda r: (str(_value(r.get(key, ""))), r["id"]))
        rows = rows[:limit] if limit else rows
        return [{"id": r["id"], **{f: r.get(f, False) for f in fields if f != "id"}} for r in rows]


class _Env(dict):
    uid = 2


def client(models, user_tz=HCM, uid=2):
    base = {
        "res.users": FakeModel([{"id": uid, "login": "admin", "tz": user_tz, "company_id": [1, "GAC"]}]),
        "product.product": FakeModel([
            {"id": 10, "default_code": "MDF-18", "name": "MDF 18mm", "product_tmpl_id": [100, "MDF 18mm"]},
            {"id": 11, "default_code": False, "name": "Sawdust", "product_tmpl_id": [101, "Sawdust"]},
        ]),
        "mrp.workcenter": FakeModel(
            [{"id": 1, "code": "PRESS-01", "name": "Hot press 1"}, {"id": 2, "code": False, "name": "SANDER-01"}],
            fields={"code", "name", "capacity", "time_start", "time_stop"},
        ),
        "mrp.workorder": FakeModel(fields={"production_id", "workcenter_id"}),
        # always there in a real Odoo (resource is a dependency of mrp)
        "resource.calendar": FakeModel(fields={"name", "tz"}),
        "resource.calendar.attendance": FakeModel(
            fields={"calendar_id", "dayofweek", "hour_from", "hour_to", "week_type", "day_period"}
        ),
    }
    env = _Env({**base, **models})
    env.uid = uid
    return SimpleNamespace(env=env, server_version="19.0")


# --------------------------------------------------------------------------
# window and time zone
# --------------------------------------------------------------------------


def test_a_local_day_becomes_its_utc_bounds():
    w = mrp._window("2026-11-13", "2026-11-13", mrp._zone(HCM))
    assert (w["utc_from"], w["utc_to"]) == ("2026-11-12 17:00:00", "2026-11-13 17:00:00")


def test_a_reversed_window_is_refused():
    with pytest.raises(ValueError, match="before"):
        mrp._window("2026-11-13", "2026-11-12", mrp._zone(HCM))


def test_tz_falls_back_to_the_odoo_user_then_utc_and_says_so():
    zone, notes = mrp.resolve_tz(client({}), None)
    assert zone.key == HCM
    assert "taken from the Odoo user" in notes[0]

    zone, notes = mrp.resolve_tz(client({}, user_tz=False), None)
    assert zone.key == "UTC"
    assert "UTC days" in notes[0]

    assert mrp.resolve_tz(client({}), "Europe/Paris") == (mrp._zone("Europe/Paris"), [])


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

_MO = {"id": 7, "name": "WH/MO/00007", "product_id": [10, "MDF 18mm"], "origin": "S00008"}
_MOVE_FIELDS = {"production_id", "product_id", "product_uom", "quantity", "picked", "date", "state"}


def _move(mid, date, qty, product=10, state="done", qty_field="quantity"):
    return {
        "id": mid,
        "production_id": [7, "WH/MO/00007"],
        "product_id": [product, ""],
        "product_uom": [1, "Units"],
        qty_field: qty,
        "date": date,
        "state": state,
    }


def test_output_is_per_mo_per_local_day_with_scrap_beside_it():
    c = client({
        "mrp.production": FakeModel([_MO]),
        "stock.move": FakeModel(
            [
                # 18:00 UTC on the 12th is 01:00 on the 13th in the plant
                _move(1, "2026-11-12 18:00:00", 100),
                _move(2, "2026-11-13 03:00:00", 50),
                _move(3, "2026-11-13 04:00:00", 999, product=11),  # by-product: not output
                _move(4, "2026-11-13 05:00:00", 70, state="assigned"),  # not done
                _move(5, "2026-11-11 10:00:00", 30),  # outside the window
            ],
            fields=_MOVE_FIELDS,
        ),
        "stock.scrap": FakeModel([
            {"id": 1, "production_id": [7, ""], "scrap_qty": 4.0, "date_done": "2026-11-13 02:00:00", "state": "done"}
        ]),
        "mrp.workorder": FakeModel(
            [{"id": 1, "production_id": [7, ""], "workcenter_id": [1, ""]}], fields={"production_id", "workcenter_id"}
        ),
    })
    result = mrp.mrp_output(c, "gac", "2026-11-13", "2026-11-13", tz=HCM)

    assert result["rows"] == [
        {
            "production_day": "2026-11-13",
            "work_order": "WH/MO/00007",
            "part_number": "MDF-18",
            "qty_good": 150.0,
            "qty_scrap": 4.0,
            "uom": "Units",
            "asset_id": "PRESS-01",
            "sales_order": "S00008",
            "odoo_ref": "odoo:mrp.production/7",
        }
    ]
    assert result["truncated"] is False


def test_output_reads_quantity_done_on_v16_and_earlier():
    c = client({
        "mrp.production": FakeModel([_MO]),
        "stock.move": FakeModel(
            [_move(1, "2026-11-13 03:00:00", 12, qty_field="quantity_done")],
            fields=(_MOVE_FIELDS - {"quantity", "picked"}) | {"quantity_done"},
        ),
        "stock.scrap": FakeModel(fields={"production_id", "scrap_qty", "date_done", "state"}),
    })
    (row,) = mrp.mrp_output(c, "gac", "2026-11-13", "2026-11-13", tz=HCM)["rows"]
    assert row["qty_good"] == 12


def test_output_without_scrap_on_the_mo_leaves_scrap_unknown_not_zero():
    c = client({
        "mrp.production": FakeModel([_MO]),
        "stock.move": FakeModel([_move(1, "2026-11-13 03:00:00", 12)], fields=_MOVE_FIELDS),
        # stock.scrap without production_id: mrp's link isn't there
        "stock.scrap": FakeModel(fields={"scrap_qty", "date_done", "state"}),
    })
    result = mrp.mrp_output(c, "gac", "2026-11-13", "2026-11-13", tz=HCM)
    assert result["rows"][0]["qty_scrap"] is None
    assert "stock.scrap.production_id" in result["missing_fields"]


def test_a_capped_pull_says_it_was_capped():
    moves = [_move(i, f"2026-11-13 0{i}:00:00", 1) for i in range(1, 5)]
    c = client({
        "mrp.production": FakeModel([_MO]),
        "stock.move": FakeModel(moves, fields=_MOVE_FIELDS),
        "stock.scrap": FakeModel(fields={"production_id", "scrap_qty", "date_done", "state"}),
    })
    assert mrp.mrp_output(c, "gac", "2026-11-13", "2026-11-13", tz=HCM, limit=3)["truncated"] is True
    assert mrp.mrp_output(c, "gac", "2026-11-13", "2026-11-13", tz=HCM, limit=4)["truncated"] is False


# --------------------------------------------------------------------------
# work orders and plan
# --------------------------------------------------------------------------


def _productions(start_field):
    base = {"product_id": [10, ""], "product_qty": 400.0, "product_uom_id": [1, "Units"], "origin": False}
    return FakeModel(
        [
            {"id": 1, "name": "MO/1", "state": "confirmed", start_field: "2026-11-13 00:00:00", **base},
            {"id": 2, "name": "MO/2", "state": "done", start_field: "2026-11-13 01:00:00", **base},
            {"id": 3, "name": "MO/3", "state": "cancel", start_field: "2026-11-13 02:00:00", **base},
            {"id": 4, "name": "MO/4", "state": "confirmed", start_field: "2026-11-14 00:00:00", **base},
        ],
        fields={"name", "product_id", "product_qty", "product_uom_id", "state", "origin", start_field},
    )


@pytest.mark.parametrize("start_field", ["date_planned_start", "date_start"])
def test_work_orders_are_open_mos_by_scheduled_start_on_either_naming(start_field):
    c = client({"mrp.production": _productions(start_field)})
    result = mrp.mrp_work_orders(c, "gac", "2026-11-13", "2026-11-13", tz=HCM)

    assert [r["work_order"] for r in result["rows"]] == ["MO/1"]
    assert result["rows"][0]["scheduled_start"] == "2026-11-13T07:00:00+07:00"
    # date_deadline, qty_produced: not on this fake, so named, not zeroed
    assert "mrp.production.date_deadline" in result["missing_fields"]
    assert result["rows"][0]["qty_produced_to_date"] is None


def test_plan_keeps_done_mos_but_not_cancelled_ones():
    c = client({"mrp.production": _productions("date_start")})
    rows = mrp.mrp_plan(c, "gac", "2026-11-13", "2026-11-13", tz=HCM)["rows"]
    assert [(r["work_order"], r["plan_date"], r["planned_qty"]) for r in rows] == [
        ("MO/1", "2026-11-13", 400.0),
        ("MO/2", "2026-11-13", 400.0),
    ]


def test_an_mo_spanning_two_work_centers_is_not_put_on_either():
    c = client({
        "mrp.production": _productions("date_start"),
        "mrp.workorder": FakeModel([
            {"id": 1, "production_id": [1, ""], "workcenter_id": [1, ""]},
            {"id": 2, "production_id": [1, ""], "workcenter_id": [2, ""]},
            {"id": 3, "production_id": [2, ""], "workcenter_id": [2, ""]},
        ]),
    })
    rows = mrp.mrp_plan(c, "gac", "2026-11-13", "2026-11-13", tz=HCM)["rows"]
    assert {r["work_order"]: r["asset_id"] for r in rows} == {"MO/1": None, "MO/2": "SANDER-01"}


# --------------------------------------------------------------------------
# downtime
# --------------------------------------------------------------------------


def test_downtime_duration_is_elapsed_time_not_odoos_calendar_minutes():
    """Odoo 19's `duration` counts an availability stop only inside the
    work center's calendar: a 90-min stop outside it reads 0 there. Seen
    live on a demo database whose calendar is Europe/Brussels while the
    plant runs in Asia/Ho_Chi_Minh."""
    c = client({
        "mrp.workcenter.productivity": FakeModel([
            {
                "id": 1,
                "workcenter_id": [1, ""],
                "loss_id": [1, ""],
                "date_start": "2026-11-13 01:00:00",
                "date_end": "2026-11-13 02:30:00",
                "duration": 0.0,
                "description": False,
            },
        ]),
        "mrp.workcenter.productivity.loss": FakeModel([
            {"id": 1, "name": "Material Availability", "loss_type": "availability"}
        ]),
    })
    (row,) = mrp.mrp_downtime(c, "gac", "2026-11-13", "2026-11-13", tz=HCM)["rows"]
    assert (row["duration_minutes"], row["working_minutes"]) == (90.0, 0.0)


def test_downtime_keeps_stops_and_slow_running_but_not_productive_time():
    c = client({
        "mrp.workcenter.productivity": FakeModel(
            [
                {
                    "id": 1,
                    "workcenter_id": [1, ""],
                    "loss_id": [1, ""],
                    "date_start": "2026-11-13 01:00:00",
                    "date_end": "2026-11-13 02:30:00",
                    "duration": 90.0,
                    "description": "waiting for veneer",
                },
                {
                    "id": 2,
                    "workcenter_id": [2, ""],
                    "loss_id": [2, ""],
                    "date_start": "2026-11-13 03:00:00",
                    "date_end": "2026-11-13 08:00:00",
                    "duration": 300.0,
                    "description": False,
                },
                {
                    "id": 3,
                    "workcenter_id": [1, ""],
                    "loss_id": [3, ""],
                    "date_start": "2026-11-13 09:00:00",
                    "date_end": False,
                    "duration": 12.0,
                    "description": False,
                },
                {
                    "id": 4,
                    "workcenter_id": [1, ""],
                    "loss_id": False,
                    "date_start": "2026-11-13 04:00:00",
                    "date_end": "2026-11-13 04:10:00",
                    "duration": 10.0,
                    "description": False,
                },
                # ended before the window opened
                {
                    "id": 5,
                    "workcenter_id": [1, ""],
                    "loss_id": [1, ""],
                    "date_start": "2026-11-10 01:00:00",
                    "date_end": "2026-11-10 02:00:00",
                    "duration": 60.0,
                    "description": False,
                },
            ],
            fields={"workcenter_id", "loss_id", "date_start", "date_end", "duration", "description", "workorder_id"},
        ),
        "mrp.workcenter.productivity.loss": FakeModel([
            {"id": 1, "name": "Material Availability", "loss_type": "availability"},
            {"id": 2, "name": "Productive", "loss_type": "productive"},
            {"id": 3, "name": "Reduced Speed", "loss_type": "performance"},
        ]),
    })
    rows = mrp.mrp_downtime(c, "gac", "2026-11-13", "2026-11-13", tz=HCM)["rows"]

    assert [(r["asset_id"], r["reason_code"], r["loss_type"], r["duration_minutes"]) for r in rows] == [
        ("PRESS-01", "Material Availability", "availability", 90.0),
        ("PRESS-01", None, None, 10.0),  # no reason code: said, not hidden in "Other"
        ("PRESS-01", "Reduced Speed", "performance", None),  # still open
    ]
    assert rows[2]["working_minutes"] is None
    assert rows[0]["started_at"] == "2026-11-13T08:00:00+07:00"
    assert rows[2]["ended_at"] is None

    with_productive = mrp.mrp_downtime(c, "gac", "2026-11-13", "2026-11-13", tz=HCM, include_productive=True)
    assert "SANDER-01" in {r["asset_id"] for r in with_productive["rows"]}


# --------------------------------------------------------------------------
# maintenance
# --------------------------------------------------------------------------


def test_maintenance_includes_requests_still_open_from_before_the_window():
    c = client({
        "maintenance.stage": FakeModel([{"id": 1, "done": False}, {"id": 2, "done": True}]),
        "maintenance.equipment": FakeModel(
            [{"id": 5, "name": "PRESS-02", "workcenter_id": [1, ""]}], fields={"name", "workcenter_id"}
        ),
        "maintenance.request": FakeModel([
            {
                "id": 12,
                "name": "Hydraulic seal",
                "equipment_id": [5, ""],
                "maintenance_type": "corrective",
                "request_date": "2026-11-10",
                "close_date": "2026-11-10",
                "stage_id": [2, "Repaired"],
                "description": "<p>Replaced seal on <b>platen 3</b></p>",
            },
            {
                "id": 15,
                "name": "Pressure drop",
                "equipment_id": [5, ""],
                "maintenance_type": "corrective",
                "request_date": "2026-11-01",
                "close_date": False,
                "stage_id": [1, "In Progress"],
                "description": False,
            },
            {
                "id": 16,
                "name": "Old and done",
                "equipment_id": [5, ""],
                "maintenance_type": "preventive",
                "request_date": "2026-11-01",
                "close_date": "2026-11-02",
                "stage_id": [2, "Repaired"],
                "description": False,
            },
            {
                "id": 17,
                "name": "Raised after",
                "equipment_id": [5, ""],
                "maintenance_type": "corrective",
                "request_date": "2026-11-20",
                "close_date": False,
                "stage_id": [1, "New"],
                "description": False,
            },
        ]),
    })
    rows = mrp.mrp_maintenance(c, "gac", "2026-11-09", "2026-11-13", tz=HCM)["rows"]

    assert [(r["maintenance_wo"], r["is_open"]) for r in rows] == [("Pressure drop", True), ("Hydraulic seal", False)]
    assert rows[1]["close_out_note"] == "Replaced seal on platen 3"
    assert rows[0]["asset_id"] == "PRESS-02"
    assert rows[0]["work_center"] == "PRESS-01"


# --------------------------------------------------------------------------
# master data
# --------------------------------------------------------------------------


def test_work_centers_carry_their_calendar_without_the_lunch_slot():
    c = client({
        "mrp.workcenter": FakeModel(
            [
                {
                    "id": 1,
                    "code": "PRESS-01",
                    "name": "Hot press 1",
                    "capacity": 2.0,
                    "time_efficiency": 100.0,
                    "oee_target": 85.0,
                    "costs_hour": 40.0,
                    "time_start": 10.0,
                    "time_stop": 5.0,
                    "resource_calendar_id": [3, "2 shifts"],
                }
            ],
            # v16 and earlier: one capacity on the work center itself
            fields={
                "code",
                "name",
                "capacity",
                "time_efficiency",
                "oee_target",
                "costs_hour",
                "time_start",
                "time_stop",
                "resource_calendar_id",
            },
        ),
        "resource.calendar": FakeModel([{"id": 3, "name": "2 shifts", "tz": HCM}]),
        "resource.calendar.attendance": FakeModel(
            [
                {
                    "id": 1,
                    "calendar_id": [3, ""],
                    "dayofweek": "0",
                    "hour_from": 6.0,
                    "hour_to": 11.0,
                    "day_period": "morning",
                },
                {
                    "id": 2,
                    "calendar_id": [3, ""],
                    "dayofweek": "0",
                    "hour_from": 11.0,
                    "hour_to": 12.0,
                    "day_period": "lunch",
                },
                {
                    "id": 3,
                    "calendar_id": [3, ""],
                    "dayofweek": "0",
                    "hour_from": 12.0,
                    "hour_to": 14.0,
                    "day_period": "afternoon",
                },
            ],
            fields={"calendar_id", "dayofweek", "hour_from", "hour_to", "day_period"},
        ),
    })
    (row,) = mrp.mrp_work_centers(c, "gac")["rows"]

    assert row["capacity"] == 2.0
    assert row["product_capacities"] is None
    assert row["calendar_tz"] == HCM
    assert [(s["day"], s["from"], s["to"]) for s in row["attendance"]] == [
        ("Monday", 6.0, 11.0),
        ("Monday", 12.0, 14.0),
    ]


def test_routings_give_the_ideal_cycle_time_per_unit():
    c = client({
        "mrp.workcenter": FakeModel([
            {"id": 1, "code": "PRESS-01", "name": "Hot press 1", "capacity": 4.0, "time_start": 15.0, "time_stop": 15.0}
        ]),
        "mrp.bom": FakeModel([
            {"id": 20, "product_tmpl_id": [100, ""], "product_id": False, "product_qty": 1.0, "code": False}
        ]),
        "product.template": FakeModel([{"id": 100, "default_code": "MDF-18", "name": "MDF 18mm"}]),
        "mrp.routing.workcenter": FakeModel([
            {
                "id": 30,
                "bom_id": [20, ""],
                "name": "Press",
                "sequence": 10,
                "workcenter_id": [1, ""],
                "time_mode": "manual",
                "time_cycle_manual": 6.0,
                "time_cycle": 6.0,
            },
        ]),
    })
    (row,) = mrp.mrp_routings(c, "gac", part_numbers=["MDF-18"])["rows"]

    assert row["part_number"] == "MDF-18"
    assert row["run_min_per_unit"] == 1.5
    assert row["setup_hours"] == 0.5


_V17_CENTER = FakeModel(
    [{"id": 1, "code": "PRESS-01", "name": "Hot press 1", "time_start": 15.0, "time_stop": 15.0}],
    fields={"code", "name", "time_start", "time_stop", "capacity_ids"},
)


def _v17_routing(capacity_rows, bom_qty=4.0):
    return client({
        "mrp.workcenter": _V17_CENTER,
        "mrp.workcenter.capacity": FakeModel(
            capacity_rows, fields={"workcenter_id", "product_id", "capacity", "time_start", "time_stop"}
        ),
        "mrp.bom": FakeModel([
            {"id": 20, "product_tmpl_id": [100, ""], "product_id": False, "product_qty": bom_qty, "code": False}
        ]),
        "product.template": FakeModel([{"id": 100, "default_code": "MDF-18", "name": "MDF 18mm"}]),
        "mrp.routing.workcenter": FakeModel([
            {
                "id": 30,
                "bom_id": [20, ""],
                "name": "Press",
                "sequence": 10,
                "workcenter_id": [1, ""],
                "time_mode": "manual",
                "time_cycle_manual": 6.0,
                "time_cycle": 6.0,
            },
        ]),
    })


def test_v17_routings_fall_back_to_the_bom_quantity_without_a_capacity_row():
    """v17+ has no capacity on the work center: `_get_capacity` falls back
    to the BoM's own quantity, with the work center's setup/cleanup."""
    (row,) = mrp.mrp_routings(_v17_routing([]), "gac")["rows"]
    assert (row["capacity"], row["run_min_per_unit"], row["setup_hours"]) == (4.0, 1.5, 0.5)


def test_v17_routings_prefer_the_products_own_capacity_row_and_its_times():
    rows = [
        {"id": 1, "workcenter_id": [1, ""], "product_id": False, "capacity": 2.0, "time_start": 0.0, "time_stop": 0.0},
        {
            "id": 2,
            "workcenter_id": [1, ""],
            "product_id": [10, ""],
            "capacity": 3.0,
            "time_start": 30.0,
            "time_stop": 6.0,
        },
    ]
    (row,) = mrp.mrp_routings(_v17_routing(rows), "gac")["rows"]
    assert (row["capacity"], row["run_min_per_unit"], row["setup_hours"]) == (3.0, 2.0, 0.6)

    # only the generic row: it applies to any product
    (row,) = mrp.mrp_routings(_v17_routing(rows[:1]), "gac")["rows"]
    assert (row["capacity"], row["run_min_per_unit"], row["setup_hours"]) == (2.0, 3.0, 0.0)


def test_v17_work_centers_list_their_per_product_capacities():
    c = _v17_routing([
        {
            "id": 2,
            "workcenter_id": [1, ""],
            "product_id": [10, ""],
            "capacity": 3.0,
            "time_start": 30.0,
            "time_stop": 6.0,
        },
    ])
    result = mrp.mrp_work_centers(c, "gac")
    (row,) = result["rows"]
    assert row["capacity"] is None
    assert row["product_capacities"] == [
        {"part_number": "MDF-18", "capacity": 3.0, "setup_minutes": 30.0, "cleanup_minutes": 6.0}
    ]
    assert "mrp.workcenter.capacity" not in result["missing_fields"]
    assert "BoM quantity" in result["notes"][0]


def test_routings_before_v14_say_why_they_are_empty():
    c = client({"mrp.routing.workcenter": FakeModel(fields={"routing_id", "workcenter_id"})})
    result = mrp.mrp_routings(c, "gac")
    assert result["rows"] == []
    assert "Odoo 14" in result["notes"][0]


def test_items_report_an_unset_cost_as_unknown_and_name_missing_parts():
    c = client({
        "product.product": FakeModel([
            {
                "id": 10,
                "default_code": "MDF-18",
                "name": "MDF 18mm",
                "product_tmpl_id": [100, ""],
                "uom_id": [1, "Units"],
                "standard_price": 0.0,
                "seller_ids": [],
            },
        ]),
        "mrp.bom": FakeModel([{"id": 20, "product_tmpl_id": [100, ""], "product_id": False}]),
    })
    result = mrp.mrp_items(c, "gac", ["MDF-18", "NOPE-1"])

    (row,) = result["rows"]
    assert row["std_cost"] is None
    assert row["make_buy"] == "make"
    assert "NOPE-1" in result["notes"][0]

    with pytest.raises(ValueError, match="never pulled whole"):
        mrp.mrp_items(c, "gac", [])


# --------------------------------------------------------------------------
# run() and the MCP gate
# --------------------------------------------------------------------------


def test_run_turns_an_odoo_error_into_a_message(monkeypatch):
    monkeypatch.setattr(mrp, "connect", lambda env: (client({}), ""))

    def boom(_client, _env):
        msg = "Object maintenance.request doesn't exist"
        raise RuntimeError(msg)

    result, message = mrp.run("gac", boom)
    assert result is None
    assert "maintenance.request" in message


def test_mrp_tools_refuse_without_the_launch_time_flag(monkeypatch):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"odooly"})
    with pytest.raises(ValueError, match="--enable-plugins=mrp"):
        mcp_server.mrp_scope("gac")


def test_mrp_tools_delegate_once_enabled(monkeypatch):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"odooly", "mrp"})
    monkeypatch.setattr(mrp, "_MODULES", ("mrp",))
    c = client({"ir.module.module": FakeModel([{"id": 1, "name": "mrp", "state": "installed"}])})
    monkeypatch.setattr(mrp, "connect", lambda env: (c, ""))

    scope = mcp_server.mrp_scope("gac")
    assert scope["installed"] == ["mrp"]
    assert scope["tz"] == HCM


def test_mrp_tools_raise_the_connection_message(monkeypatch):
    monkeypatch.setattr(mcp_server, "_enabled_plugins", {"mrp"})
    monkeypatch.setattr(mrp, "connect", lambda env: (None, f"cannot connect to '{env}': refused"))
    with pytest.raises(ValueError, match="refused"):
        mcp_server.mrp_downtime("gac", "2026-11-13", "2026-11-13")
