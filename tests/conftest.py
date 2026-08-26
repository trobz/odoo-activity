import pytest

from odoo_activity import probes, tui


@pytest.fixture(autouse=True)
def _no_neutralization_probe(monkeypatch):
    """Keep the db-row neutralization probe off the real shell.

    The app tests stub `databases_of`; without this the follow-up probe
    would still run `systemctl` and `psql` against whatever box the suite
    happens to run on -- slow, answering about the developer's own
    databases, and silent about which of them it even reached. A test that
    is about the tag sets its own stubs on top.

    In `conftest.py` rather than one test module: `test_odooly.py` drives
    the same app and was missed when this lived in `test_tui.py`, which is
    how it reached CI.
    """
    monkeypatch.setattr(tui, "pg_target_of", lambda *_a, **_k: probes.PgTarget())
    monkeypatch.setattr(tui, "neutralization_of", lambda *_a, **_k: {})
