import pytest

from odoo_activity import plugins


def _plugin(name: str, *, default: bool = False) -> plugins.Plugin:
    p = plugins.Plugin()
    p.name = name
    p.default = default
    return p


def test_select_omitted_flags_runs_only_default_on_plugins():
    installed = [_plugin("odooly", default=True), _plugin("other")]
    assert [p.name for p in plugins.select(installed, [], [])] == ["odooly"]


def test_select_enable_overrides_defaults_including_non_default_plugins():
    installed = [_plugin("odooly", default=True), _plugin("other")]
    assert [p.name for p in plugins.select(installed, ["other"], [])] == ["other"]


def test_select_disable_wins_over_default():
    installed = [_plugin("odooly", default=True)]
    assert plugins.select(installed, [], ["odooly"]) == []


def test_select_unknown_name_raises():
    installed = [_plugin("odooly", default=True)]
    with pytest.raises(plugins.UnknownPlugin):
        plugins.select(installed, ["bogus"], [])
