"""Matching instances/databases to odooly environments, and the scripts.

The matching is all string work over the user's own `~/odooly.ini`, so it is
tested against written-out ini files rather than a live instance -- what
matters is which section a (instance, database) pair resolves to.
"""

from types import SimpleNamespace
from typing import cast

from odoo_activity import probes

_INI = """\
[acme18-int]
database = acme18_int
username = admin

[acme18-int-db2]
database = db2
username = admin

[acme18-staging]
username = admin

[other-prod]
database = other
username = admin
"""


def _envs(tmp_path, text=_INI):
    path = tmp_path / "odooly.ini"
    path.write_text(text)

    return probes.read_odooly_envs(path)


def test_reads_only_the_name_and_database_of_each_env(tmp_path):
    """The file holds passwords and api keys too. Matching needs neither, and
    what isn't read can't end up on a screen or in a log."""
    envs = _envs(tmp_path, "[demo]\ndatabase = demo_db\nusername = admin\npassword = hunter2\n")

    assert envs == [{"name": "demo", "db": "demo_db"}]


def test_missing_or_broken_ini_is_not_an_error(tmp_path):
    """Odooly support is opt-in and best-effort — a file the user never wrote
    (or wrote badly) must not take the app down."""
    assert probes.read_odooly_envs(tmp_path / "absent.ini") == []
    assert _envs(tmp_path, "this is not an ini") == []


def test_matches_an_instance_to_its_env_however_it_is_spelled(tmp_path):
    """Instances are named after the environment they serve, odooly sections
    after the same thing — abbreviated or not, with the manager's prefix
    dropped, and with the database appended for a multi-db instance."""
    envs = _envs(tmp_path)
    match = probes.match_odooly_env

    # the manager's own prefix/suffix, and `integration` spelled either way
    assert match("openerp-acme18-integration.service", "acme18_int", envs) == "acme18-int"
    assert match("odoo-acme18-int", "acme18_int", envs) == "acme18-int"

    # a second database on the same instance, configured as its own section
    assert match("openerp-acme18-integration", "db2", envs) == "acme18-int-db2"

    # an env that pins no database still serves whatever that instance runs
    assert match("openerp-acme18-staging", "anything", envs) == "acme18-staging"

    # ...but one that pins a different database does not
    assert match("openerp-other-production", "not-other", envs) is None
    assert match("openerp-elsewhere-int", "acme18_int", envs) is None


def test_a_db_pinned_env_wins_over_a_looser_one(tmp_path):
    """Both sections match the instance; only one names this database, and
    that is the one whose credentials belong to it."""
    envs = _envs(tmp_path, "[demo-int]\nusername = admin\n\n[demo-int-db1]\ndatabase = db1\nusername = admin\n")

    assert probes.match_odooly_env("openerp-demo-integration", "db1", envs) == "demo-int-db1"
    assert probes.match_odooly_env("openerp-demo-integration", "db9", envs) == "demo-int"


def test_scripts_run_in_this_interpreter_and_report_what_they_printed(monkeypatch):
    """The scripts ship inside the package and need odooly, so they run under
    the interpreter already running odoo-activity — never over `Host`, since
    odooly reaches the instance from *this* machine's ~/odooly.ini."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(stdout="Complete — 12 menu(s) restored.\n", stderr="", returncode=0)

    monkeypatch.setattr(probes.subprocess, "run", fake_run)

    assert probes.run_odooly_script("restore_app_icons", "acme18-int") == "Complete — 12 menu(s) restored."
    assert seen["argv"][1:] == ["-m", "odoo_activity.scripts.restore_app_icons", "--env", "acme18-int"]
    assert seen["argv"][0] == probes.sys.executable


def test_run_odooly_script_appends_extra_args_after_env(monkeypatch):
    """send_test_mail needs a --to the caller supplies -- appended after
    --env, in the order the standalone CLI itself expects."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(
            stdout="Test mail sent on acme18-int: mail.mail #9, to a@example.com\n", stderr="", returncode=0
        )

    monkeypatch.setattr(probes.subprocess, "run", fake_run)

    result = probes.run_odooly_script("send_test_mail", "acme18-int", "--to", "a@example.com")

    assert "mail.mail #9" in result
    assert seen["argv"][1:] == [
        "-m",
        "odoo_activity.scripts.send_test_mail",
        "--env",
        "acme18-int",
        "--to",
        "a@example.com",
    ]


def test_a_failing_script_shows_its_error_rather_than_nothing(monkeypatch):
    monkeypatch.setattr(
        probes.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout="", stderr="no such odooly env: 'nope'\n", returncode=1),
    )

    assert probes.run_odooly_script("create_test_job", "nope") == "no such odooly env: 'nope'"

    def timeout(*_a, **_k):
        raise probes.subprocess.TimeoutExpired(cmd="x", timeout=300)

    monkeypatch.setattr(probes.subprocess, "run", timeout)
    assert "timed out" in probes.run_odooly_script("create_test_job", "slow")


def test_the_marker_lands_in_the_same_column_as_an_instances_status():
    """A db row is nested under its instance, so a marker that starts right
    after the db name lands wherever that name happens to end. It belongs
    where the eye already reads a state: the instance rows' status column."""
    from odoo_activity import tui

    name_width, uptime_width = 24, 10
    instance = f"● {'openerp-demo-integration':<{name_width}} {'0:01':>{uptime_width}}  RUNNING"

    for db, port in (("demo_db", "5432"), ("a-much-longer-database-name", None)):
        label = tui._db_label(db, port, name_width, uptime_width, indent=4)
        row = f"  └── {label}  ODOOLY".replace("[dim]", "").replace("[/]", "")
        assert row.index("ODOOLY") == instance.index("RUNNING")


def _row_text(item) -> str:
    """The rendered text of one instances-list row."""
    from textual.widgets import Label

    return str(item.query_one(Label).render())


def _odooly_pilot(monkeypatch, envs):
    """An app on one instance with one db, with `envs` standing in for what
    `--enable-odooly` would have read out of ~/odooly.ini."""
    from odoo_activity import tui
    from odoo_activity.panes import detail as detail_mod

    instances = [
        {"name": "openerp-demo-integration.service", "status": "running", "uptime": "0:01", "manager": "systemd"}
    ]
    monkeypatch.setattr(tui, "list_instances", lambda *_: instances)
    monkeypatch.setattr(probes, "procs_of", lambda *_: [])
    monkeypatch.setattr(tui, "databases_of", lambda *_: (["demo_db"], None))
    monkeypatch.setattr(detail_mod, "pg_target_of", lambda *_a, **_k: probes.PgTarget())
    monkeypatch.setattr(tui, "read_odooly_envs", lambda *_: envs)


def test_a_matched_database_is_marked_and_offers_the_odooly_tools(monkeypatch):
    """The marker is the whole signal that odooly can reach this database —
    and the tools that need a login appear with it."""
    import asyncio

    from odoo_activity import tui
    from odoo_activity.panes import detail as detail_mod

    _odooly_pilot(monkeypatch, [{"name": "demo-int", "db": "demo_db"}])
    monkeypatch.setattr(detail_mod, "job_groups", lambda *_: ([], ""))

    async def go():
        async with tui.OdooActivity(enable_odooly=True).run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            rows = pilot.app.query_one("#instances", tui.ListView).children
            assert "ODOOLY" in _row_text(rows[1])

            await pilot.press("down")  # onto the db row
            await pilot.pause()
            assert cast("tui.OdooActivity", pilot.app).highlighted_odooly_env() == "demo-int"

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Toolbox")
            for _ in range(20):
                await pilot.pause()

            table = pane.query_one("#actable", detail_mod.DataTable)
            assert table.row_count == len(pane.DB_TOOLBOX_TOOLS)
            assert "demo-int" in str(next(iter(table.columns.values())).label)

            # the copied command carries the config path: odooly's own CLI
            # looks for `odooly.ini` in the working directory, so the bare
            # `--env` only works from whichever folder happens to hold one
            copied = []
            monkeypatch.setattr(detail_mod, "try_local_clipboard", lambda cmd: copied.append(cmd) or True)
            table.action_select_cursor()  # enter on "Open odooly"
            for _ in range(20):
                await pilot.pause()

            assert copied == [f"odooly -c {probes.ODOOLY_CONFIG} --env demo-int"]

    asyncio.run(go())


_MAIL_AUDIT = {
    "config_parameters": [{"key": "mail.catchall.domain", "value": "example.com", "explanation": ""}],
    "alias_domains": None,
    "addresses": [],
    "mail_servers": [],
    "modules": [],
}


class _FakeMailProc:
    """Stands in for the odoo-db subprocess `mail` command's output."""

    def communicate(self, timeout=None):
        import json

        return json.dumps(_MAIL_AUDIT), ""

    def kill(self) -> None:
        pass


def test_mail_tab_offers_send_test_mail_only_with_a_matching_odooly_env(monkeypatch):
    """Same gating as Jobs' Create test job -- the button needs a login, so
    it only appears once odooly can actually reach this database. Pressing
    it prompts for a recipient (a plain yes/no confirm can't collect one) and
    only then runs the script, with the typed address passed through."""
    import asyncio

    from odoo_activity import tui
    from odoo_activity.panes import detail as detail_mod
    from odoo_activity.panes.confirm import PromptScreen

    _odooly_pilot(monkeypatch, [{"name": "demo-int", "db": "demo_db"}])
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: _FakeMailProc())

    sent = []

    def fake_run_odooly_script(*args, **_kwargs):
        sent.append(args)
        return "Test mail sent on demo-int: mail.mail #9, to a@example.com"

    monkeypatch.setattr(detail_mod, "run_odooly_script", fake_run_odooly_script)

    async def go():
        async with tui.OdooActivity(enable_odooly=True).run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("down")  # onto the db row
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Mail")
            for _ in range(20):
                await pilot.pause()

            bar = pane.query_one("#acactions", detail_mod.Horizontal)
            assert bar.display is True
            buttons = list(bar.query(detail_mod.Button))
            assert [b.id for b in buttons] == ["check-port-25", "send-test-mail"]

            buttons[1].press()
            await pilot.pause()

            assert isinstance(pilot.app.screen, PromptScreen)
            assert sent == []  # nothing runs before an address is given

            pilot.app.screen.query_one("#prompt-input", detail_mod.Input).value = "a@example.com"
            await pilot.click("#prompt-ok")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert sent == [("send_test_mail", "demo-int", "--to", "a@example.com")]

    asyncio.run(go())


def test_send_test_mail_button_is_cancelled_by_an_empty_prompt(monkeypatch):
    """Cancel, escape, or just pressing Send with nothing typed all read the
    same: nothing to send to, so nothing runs."""
    import asyncio

    from odoo_activity import tui
    from odoo_activity.panes import detail as detail_mod
    from odoo_activity.panes.confirm import PromptScreen

    _odooly_pilot(monkeypatch, [{"name": "demo-int", "db": "demo_db"}])
    monkeypatch.setattr(detail_mod, "start_odoo_db", lambda *_a, **_k: _FakeMailProc())

    sent = []
    monkeypatch.setattr(detail_mod, "run_odooly_script", lambda *args, **_k: sent.append(args))

    async def go():
        async with tui.OdooActivity(enable_odooly=True).run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Mail")
            for _ in range(20):
                await pilot.pause()

            pane.query_one("#send-test-mail", detail_mod.Button).press()
            await pilot.pause()
            assert isinstance(pilot.app.screen, PromptScreen)

            await pilot.click("#prompt-cancel")
            await pilot.pause()

            assert sent == []

    asyncio.run(go())


def test_without_a_match_the_marker_turns_red_and_nothing_odooly_is_offered(monkeypatch):
    """No section for this database: the marker is still there, in red, and a
    Toolbox that says why instead of listing tools that would only fail."""
    import asyncio

    from odoo_activity import tui
    from odoo_activity.panes import detail as detail_mod

    _odooly_pilot(monkeypatch, [{"name": "elsewhere-int", "db": "other"}])

    async def go():
        async with tui.OdooActivity(enable_odooly=True).run_test(size=(100, 40)) as pilot:
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            rows = pilot.app.query_one("#instances", tui.ListView).children
            assert "ODOOLY" in _row_text(rows[1])
            assert tui._odooly_marker(None) == "[red]ODOOLY[/]"

            await pilot.press("down")
            await pilot.pause()
            assert cast("tui.OdooActivity", pilot.app).highlighted_odooly_env() is None

            pane = pilot.app.query_one(tui.ActivityPane)
            pane.select_tab_by_name("Toolbox")
            for _ in range(20):
                await pilot.pause()

            assert pane.query_one("#actable", detail_mod.DataTable).row_count == 0

    asyncio.run(go())


def test_errors_never_echo_the_password_the_ini_hides_in_the_url():
    """`odooly.ini` documents `host = user:password@odoo.example.com`, so
    the server URL carries the password — and libraries quote what they were
    given: urllib answers a bad one with `nonnumeric port: 's3cr3t@host'`.
    These messages land in a terminal and in odoo-activity's pane."""
    from odoo_activity.scripts import redact

    assert redact("nonnumeric port: 's3cr3t@host.invalid'", "s3cr3t") == "nonnumeric port: '***@host.invalid'"
    assert redact("<urlopen error https://bob:hunter2@host/x>") == "<urlopen error https://***:***@host/x>"
    assert redact("plain failure", "") == "plain failure"  # no password configured, nothing to mask

    # configparser quotes the line it choked on, which may be the secret
    # itself -- and at that point nothing has parsed, so no password is known
    assert "hunter2" not in redact("parsing errors: [line 2]: 'password hunter2'")
    assert "abc123" not in redact("api_key = abc123 rejected")


class _FakeMenus:
    """Stands in for `client.env["ir.ui.menu"]` — records what was written."""

    def __init__(self, menus):
        self._menus = menus
        self.written = []

    def search_read(self, _domain, _fields):
        return self._menus

    def write(self, menu_id, values):
        self.written.append((menu_id, values))


def _client(menus):
    return SimpleNamespace(env={"ir.ui.menu": _FakeMenus(menus)})


def test_only_the_menus_whose_icon_is_actually_gone_are_rewritten():
    """Writing `web_icon` back is what makes Odoo recompute the image, so
    doing it to a menu that is fine rewrites a good attachment and orphans
    its file. A lost filestore leaves the attachment row in place with only
    the file missing, which `_file_read` answers with empty bytes — so the
    data, not the row, is what says a menu is broken."""
    from odoo_activity.scripts.restore_app_icons import restore_app_icons

    menus = [
        {"id": 1, "name": "Apps", "web_icon": "base,static/description/modules.png", "web_icon_data": False},
        {"id": 2, "name": "Settings", "web_icon": "base,static/description/settings.png", "web_icon_data": ""},
        {"id": 3, "name": "Discuss", "web_icon": "mail,static/description/icon.png", "web_icon_data": "aVZCT1J3MEs="},
    ]
    client = _client(menus)

    assert restore_app_icons(client) == (2, 3)
    assert [menu_id for menu_id, _values in client.env["ir.ui.menu"].written] == [1, 2]

    # ...and a second run has nothing left to do
    client = _client([dict(menu, web_icon_data="aVZCT1J3MEs=") for menu in menus])
    assert restore_app_icons(client) == (0, 3)
    assert client.env["ir.ui.menu"].written == []


class _FakeMailRecord:
    """Stands in for odooly's `Record` -- what `create()` already returns
    (see `_FakeMailModel.create`), carrying `.id` and a `.send()` bound to
    this one record, same as the real thing."""

    def __init__(self, model: "_FakeMailModel", record_id: int) -> None:
        self._model = model
        self.id = record_id

    def send(self):
        self._model.sent_ids.append(self.id)
        return True


class _FakeMailModel:
    """Stands in for one `client.env[model]` — `search_read` returns canned
    rows; `create` returns a `_FakeMailRecord` exactly like odooly's own
    `create()` does (it calls `browse()` internally and returns the
    `Record`, not a plain id). `browse()` itself raises: calling it on what
    `create()` already returned is the real bug this fixture is shaped to
    catch (odooly's `RecordList.__init__` asserts an int and raises
    `AssertionError: <Record 'mail.mail,N'>` on that call, caught live
    against a real host -- see send_test_mail.py's `send_test_mail`)."""

    def __init__(self, rows=(), *, send_state="sent", failure_reason=None):
        self._rows = rows
        self.created = []
        self.sent_ids: list[int] = []
        self._next_id = 7
        # What the post-send() search_read([...], ["state", "failure_reason"])
        # in send_test_mail() reads back -- distinct from `rows`, which this
        # same fixture class also serves for res.users/res.company lookups.
        self._send_state = send_state
        self._failure_reason = failure_reason

    def search_read(self, _domain, fields):
        if fields == ["state", "failure_reason"]:
            return [{"state": self._send_state, "failure_reason": self._failure_reason}]
        return self._rows

    def with_context(self, **_kwargs):
        return self

    def create(self, values):
        self.created.append(values)
        record = _FakeMailRecord(self, self._next_id)
        self._next_id += 1
        return record

    def browse(self, _id):
        msg = "browse() should not be called on what create() already returned as a Record"
        raise AssertionError(msg)


class _FakeEnv(dict):
    """Stands in for odooly's `Env` -- subscriptable by model name, like a
    dict, but also carries the connected `.uid` odooly sets there (not on
    the client itself)."""

    def __init__(self, models: dict, uid: int) -> None:
        super().__init__(models)
        self.uid = uid


def _mail_client(company_email="acme@example.com", *, send_state="sent", failure_reason=None):
    users = _FakeMailModel([{"company_id": [3, "Acme"]}])
    company_rows = [{"email": company_email}] if company_email is not None else []
    env = _FakeEnv(
        {
            "res.users": users,
            "res.company": _FakeMailModel(company_rows),
            "mail.mail": _FakeMailModel(send_state=send_state, failure_reason=failure_reason),
        },
        uid=42,
    )
    return SimpleNamespace(env=env)


def test_company_email_reads_the_connecting_users_own_company():
    """`from` needs no asking: it's the same address a real notification from
    this login would carry."""
    from odoo_activity.scripts.send_test_mail import _company_email

    assert _company_email(_mail_client("acme@example.com")) == "acme@example.com"
    assert _company_email(_mail_client(company_email=None)) is None


def test_send_test_mail_creates_and_sends_one_mail():
    from odoo_activity.scripts.send_test_mail import send_test_mail

    client = _mail_client("acme@example.com")

    assert send_test_mail(client, "you@example.com") == 7
    assert client.env["mail.mail"].created == [
        {
            "email_from": "acme@example.com",
            "email_to": "you@example.com",
            "subject": "This is a test email from Trobz",
            "body_html": "Please ignore",
            "auto_delete": False,
        }
    ]
    assert client.env["mail.mail"].sent_ids == [7]


def test_send_test_mail_omits_email_from_when_the_company_has_none():
    # Passing the key with None is not the same as omitting it: Odoo only
    # fills a field's default when it's *absent* from values, so an explicit
    # None here would suppress mail.message.default_get's own sender
    # fallback and the mail would go out with no `from` address at all.
    from odoo_activity.scripts.send_test_mail import send_test_mail

    client = _mail_client(company_email=None)

    send_test_mail(client, "you@example.com")

    (created,) = client.env["mail.mail"].created
    assert "email_from" not in created


def test_send_test_mail_raises_when_the_record_ends_in_exception_state():
    # mail.send() defaults to raise_exception=False -- a failed delivery is
    # recorded on the mail (state='exception' + failure_reason) rather than
    # raised, so without reading it back a failed send would still return
    # normally and be reported as sent, the one outcome this script exists
    # to catch.
    import pytest

    from odoo_activity.scripts.send_test_mail import send_test_mail

    client = _mail_client(send_state="exception", failure_reason="Connection refused")

    with pytest.raises(RuntimeError, match="Connection refused"):
        send_test_mail(client, "you@example.com")


def test_send_test_mail_raises_a_generic_message_when_no_failure_reason_recorded():
    import pytest

    from odoo_activity.scripts.send_test_mail import send_test_mail

    client = _mail_client(send_state="exception", failure_reason=None)

    with pytest.raises(RuntimeError, match="no reason recorded"):
        send_test_mail(client, "you@example.com")
