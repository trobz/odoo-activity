"""pos.config session status, and the plugin wiring around it.

Like test_odooly.py's odooly tests, `pos_status` is tested against a fake
odooly client rather than a live instance -- what matters is which fields it
reads and how it turns them into rows, not the RPC itself.
"""

from types import SimpleNamespace

from odoo_activity.plugins import pos as pos_plugin


class _FakeConfigs:
    """Stands in for `client.env["pos.config"]`. `available_fields` is what
    `fields_get()` reports -- a subset of `rows`' keys simulates an older
    instance missing a field, the way `other_devices` is absent before v15."""

    def __init__(self, rows, available_fields):
        self._rows = rows
        self._available_fields = available_fields

    def fields_get(self):
        return {f: {} for f in self._available_fields}

    def search_read(self, _domain, fields):
        return [{"id": row["id"], **{f: row.get(f) for f in fields}} for row in self._rows]


class _FakeSessions:
    """Stands in for `client.env["pos.session"]` -- `by_config` holds each
    config's sessions (each already carrying its own `id`/`name`/`state`/
    `start_at`), newest-first, the way `order="start_at desc"` would return
    them once merged across every config in one `search_read`, the way
    `_latest_sessions` issues it."""

    def __init__(self, by_config):
        self._by_config = by_config

    def search_read(self, domain, _fields, order=None):
        assert order == "start_at desc"
        config_ids = domain[0][2]
        rows = [
            {**session, "config_id": (config_id, "")}
            for config_id in config_ids
            for session in self._by_config.get(config_id, [])
        ]
        return sorted(rows, key=lambda r: r["start_at"], reverse=True)


class _FakeOrders:
    """Stands in for `client.env["pos.order"]`. `counts` maps session id to
    how many orders it should report for `_order_counts_by_session`'s
    `search_read` (grouped in Python, one order dict per unit of count);
    `by_session` maps session id to its orders (any order -- `read_group`
    aggregates the max itself) for `_latest_order_dates`'s `read_group`.

    Grouped by `session_id`, never `config_id`: on some versions (Odoo 12's
    schema, seen live) `config_id` on `pos.order` is a related field, which
    `read_group` refuses to group by even though it's stored -- `session_id`
    is always a plain column, so that's the only field either this fake or
    the real RPC ever groups by. `read_group` rather than `search_read` for
    the date_order side is the other half of the fix this stands in for: a
    real `pos.order` can hold millions of rows, and only the aggregate --
    one row per session -- may ever cross the wire.
    """

    def __init__(self, counts=None, by_session=None):
        self._counts = counts or {}
        self._by_session = by_session or {}

    def search_read(self, domain, fields, order=None):
        key, operator, value = domain[0]
        assert key == "session_id" and operator == "in"
        return [
            {"session_id": (session_id, "")} for session_id in value for _ in range(self._counts.get(session_id, 0))
        ]

    def read_group(self, domain, fields, groupby):
        key, operator, value = domain[0]
        assert key == "session_id" and operator == "in"
        assert fields == ["session_id", "date_order:max"]
        assert groupby == ["session_id"]

        return [
            {"session_id": (session_id, ""), "date_order": max(o["date_order"] for o in orders)}
            for session_id in value
            if (orders := self._by_session.get(session_id))
        ]


class _FakePaymentMethods:
    """Stands in for `client.env["pos.payment.method"]`. `has_wait_field`
    simulates an instance without OCA's pos_payment_terminal_return module,
    where `oca_payment_terminal_return` isn't a field at all."""

    def __init__(self, rows, has_wait_field=True):
        self._rows = rows
        self._has_wait_field = has_wait_field

    def fields_get(self):
        fields = {"name": {}}
        if self._has_wait_field:
            fields["oca_payment_terminal_return"] = {}
        return fields

    def search_read(self, domain, fields):
        ids = domain[0][2]
        return [{"id": row["id"], **{f: row.get(f) for f in fields}} for row in self._rows if row["id"] in ids]


def _client(
    configs,
    available_fields=("name", "is_posbox", "proxy_ip", "other_devices", "payment_method_ids"),
    sessions=None,
    order_counts=None,
    orders_by_session=None,
    payment_methods=(),
    payment_methods_installed=True,
    payment_method_model=None,
):
    return SimpleNamespace(
        env={
            "pos.config": _FakeConfigs(configs, available_fields),
            "pos.session": _FakeSessions(sessions or {}),
            "pos.order": _FakeOrders(order_counts, orders_by_session),
            "pos.payment.method": payment_method_model
            or _FakePaymentMethods(payment_methods, payment_methods_installed),
        }
    )


def _config(config_id=1, name="Caisse 01", **extra):
    return {"id": config_id, "name": name, "payment_method_ids": [], **extra}


def test_a_config_with_no_session_ever_is_closed_with_nothing_to_report():
    client = _client([_config()])
    (row,) = pos_plugin.pos_status(client)

    assert row["status"] == "closed"
    assert row["orders"] is None
    assert row["latest_session"] is None
    assert row["open_time"] is None
    assert row["latest_order"] is None


def test_a_closed_latest_session_still_reports_its_open_time_and_order_count():
    client = _client(
        [_config()],
        sessions={1: [{"id": 501, "name": "POS/0001", "state": "closed", "start_at": "2026-01-01 08:00:00"}]},
        order_counts={501: 4},
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["status"] == "closed"
    assert row["latest_session"] == "POS/0001"
    assert row["open_time"] == "2026-01-01 08:00:00"
    assert row["orders"] == 4


def test_an_open_latest_session_reports_its_order_count():
    client = _client(
        [_config()],
        sessions={1: [{"id": 502, "name": "POS/0002", "state": "opened", "start_at": "2026-02-01 09:00:00"}]},
        order_counts={502: 7},
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["status"] == "open"
    assert row["orders"] == 7


def test_latest_order_is_independent_of_session_state():
    """A session can open and close with nothing rung up on it -- the latest
    order's own date is what says a till was actually used, not just opened."""
    client = _client(
        [_config()],
        sessions={1: [{"id": 501, "name": "POS/0001", "state": "closed", "start_at": "2026-01-01 08:00:00"}]},
        orders_by_session={501: [{"date_order": "2026-01-01 08:15:00"}]},
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["latest_order"] == "2026-01-01 08:15:00"


def test_latest_order_is_none_without_any_orders():
    client = _client([_config()])
    (row,) = pos_plugin.pos_status(client)

    assert row["latest_order"] is None


def test_latest_order_looks_past_the_latest_session_when_it_has_nothing_rung_up():
    """The latest session can be a config's *emptiest* one -- a till opened
    and closed right away leaves the real latest order sitting in an older
    session, which the config/session_id-only `read_group` can only find by
    rolling up every one of the config's sessions, not just its newest."""
    client = _client(
        [_config()],
        sessions={
            1: [
                {"id": 502, "name": "POS/0002", "state": "closed", "start_at": "2026-02-01 09:00:00"},
                {"id": 501, "name": "POS/0001", "state": "closed", "start_at": "2026-01-01 08:00:00"},
            ]
        },
        orders_by_session={501: [{"date_order": "2026-01-01 08:15:00"}]},
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["latest_session"] == "POS/0002"
    assert row["latest_order"] == "2026-01-01 08:15:00"


class _UntouchedModel:
    """Stands in for a model that shouldn't be reached at all -- raises if
    any RPC method is called on it."""

    def __getattr__(self, name):
        def fail(*_args, **_kwargs):
            msg = f"{name}() should never be called on this model"
            raise AssertionError(msg)

        return fail


def test_works_against_an_odoo_12_shaped_instance():
    """v12/v13 pos.config has `is_posbox`/`proxy_ip`/the `iface_*` booleans
    (including `iface_payment_terminal_return`, the pre-v14 home of the
    wait-for-return flag -- see `_CONFIG_WAIT_FIELD`) but no
    `other_devices`, and no `pos.payment.method` model at all -- payment is
    plain `journal_ids` there instead (see pos_config.py in a real v12
    checkout). This mirrors that shape end to end and confirms
    `pos.payment.method` is never even reached, and that the wait flag still
    comes through despite there being no payment method to name it on.

    v12 is also where `pos.order.config_id` is a related field, which is
    why `_latest_order_dates` groups by `session_id` rather than
    `config_id` -- `_FakeOrders.read_group` asserts exactly that, so this
    test would fail the same way a real v12 does if that regressed."""
    config = {
        "id": 1,
        "name": "Caisse 01",
        "is_posbox": True,
        "proxy_ip": "10.0.0.5",
        "iface_cashdrawer": True,
        "iface_electronic_scale": False,
        "iface_print_via_proxy": True,
        "iface_scan_via_proxy": False,
        "iface_payment_terminal_return": True,
    }
    client = _client(
        [config],
        available_fields=(
            "name",
            "is_posbox",
            "proxy_ip",
            "iface_cashdrawer",
            "iface_electronic_scale",
            "iface_print_via_proxy",
            "iface_scan_via_proxy",
            "iface_payment_terminal_return",
            # no other_devices, no payment_method_ids -- both v14+/v15+
        ),
        sessions={1: [{"id": 900, "name": "POS/0900", "state": "closed", "start_at": "2026-01-01 08:00:00"}]},
        order_counts={900: 12},
        orders_by_session={900: [{"date_order": "2026-01-01 08:30:00"}]},
        payment_method_model=_UntouchedModel(),
    )

    (row,) = pos_plugin.pos_status(client)

    assert row["status"] == "closed"
    assert row["orders"] == 12
    assert row["latest_session"] == "POS/0900"
    assert row["open_time"] == "2026-01-01 08:00:00"
    assert row["latest_order"] == "2026-01-01 08:30:00"
    assert row["iot_box"] is True
    assert row["proxy_ip"] == "10.0.0.5"
    assert row["other_devices"] is None
    assert row["payment_methods"] is None
    assert row["payment_terminal_wait"] is True


def test_config_level_wait_flag_is_blank_when_the_module_is_absent_too():
    client = _client(
        [_config()],
        available_fields=("name", "is_posbox", "proxy_ip"),  # no payment_method_ids, no iface_payment_terminal_return
        payment_method_model=_UntouchedModel(),
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["payment_terminal_wait"] is None


def test_a_field_missing_on_an_older_instance_is_blank_not_a_crash():
    """`other_devices` is absent before v15 -- fields_get() won't report it,
    so it must never be a KeyError (the bug hit live against
    foodcoop12-staging-caravane)."""
    client = _client(
        [_config(is_posbox=True, proxy_ip="http://localhost:8069")],
        available_fields=("name", "is_posbox", "proxy_ip", "payment_method_ids"),  # no other_devices
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["other_devices"] is None
    assert row["proxy_ip"] == "http://localhost:8069"


def test_iot_box_off_is_explicit_rather_than_inferred_from_proxy_ip():
    client = _client([_config(is_posbox=False, proxy_ip="http://localhost:8069")])
    (row,) = pos_plugin.pos_status(client)

    assert row["iot_box"] is False
    # proxy_ip is still reported as-is -- it's `iot_box` that says whether it matters
    assert row["proxy_ip"] == "http://localhost:8069"


def test_every_payment_method_on_a_config_is_named_explicitly():
    client = _client(
        [_config(payment_method_ids=[10, 11])],
        payment_methods=[
            {"id": 10, "name": "CB 01", "oca_payment_terminal_return": True},
            {"id": 11, "name": "Cash", "oca_payment_terminal_return": False},
        ],
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["payment_methods"] == "CB 01, Cash"


def test_a_config_with_no_payment_methods_reports_none():
    client = _client([_config(payment_method_ids=[])])
    (row,) = pos_plugin.pos_status(client)

    assert row["payment_methods"] is None


def test_payment_methods_waiting_for_terminal_return_are_named():
    client = _client(
        [_config(payment_method_ids=[10, 11])],
        payment_methods=[
            {"id": 10, "name": "CB 01", "oca_payment_terminal_return": True},
            {"id": 11, "name": "Cash", "oca_payment_terminal_return": False},
        ],
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["payment_terminal_wait"] == "CB 01"


def test_payment_methods_are_still_named_when_the_wait_flag_module_is_absent():
    """The wait flag is an optional OCA extra -- the methods themselves are
    core `pos.payment.method` data and should show regardless."""
    client = _client(
        [_config(payment_method_ids=[10])],
        payment_methods=[{"id": 10, "name": "CB 01"}],  # no wait flag on the row -- the field doesn't exist here
        payment_methods_installed=False,
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["payment_methods"] == "CB 01"
    assert row["payment_terminal_wait"] is None


def test_payment_columns_are_blank_without_payment_method_ids_on_this_version():
    client = _client(
        [_config()],
        available_fields=("name", "is_posbox", "proxy_ip"),  # no payment_method_ids
    )
    (row,) = pos_plugin.pos_status(client)

    assert row["payment_methods"] is None
    assert row["payment_terminal_wait"] is None


def test_rows_are_sorted_by_config_name():
    client = _client([_config(config_id=1, name="Caisse 02"), _config(config_id=2, name="Caisse 01")])
    rows = pos_plugin.pos_status(client)

    assert [row["name"] for row in rows] == ["Caisse 01", "Caisse 02"]


def _plugin(envs):
    plugin = pos_plugin.PosPlugin()
    plugin.envs = envs
    return plugin


def test_db_tab_is_pos():
    assert _plugin([]).db_tab() == "POS"


def test_env_for_matches_the_same_way_odooly_does():
    plugin = _plugin([{"name": "demo-int", "db": "demo_db"}])
    inst = {"name": "openerp-demo-integration.service"}

    assert plugin.env_for((inst, "demo_db")) == "demo-int"
    assert plugin.env_for((inst, "other_db")) is None


def test_fetch_tab_without_a_matching_env_explains_rather_than_fails():
    plugin = _plugin([])
    inst = {"name": "openerp-demo-integration.service"}

    rows, message = plugin.fetch_tab("POS", (inst, "demo_db"))

    assert rows is None
    assert message == "(no odooly env for this database)"
