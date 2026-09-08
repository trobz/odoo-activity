import pytest

from odoo_activity import plugins


def _plugin(name: str, *, default: bool = False, requires: tuple[str, ...] = ()) -> plugins.Plugin:
    p = plugins.Plugin()
    p.name = name
    p.default = default
    p.requires = requires
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


def test_select_pulls_in_requirements_transitively():
    """Naming `pos` also runs `odooly` -- without it, pos would silently
    lose the ODOOLY marker/Toolbox a database row otherwise carries."""
    installed = [_plugin("odooly"), _plugin("pos", requires=("odooly",))]
    assert {p.name for p in plugins.select(installed, ["pos"], [])} == {"odooly", "pos"}


def test_select_requirements_chain_through_multiple_plugins():
    installed = [_plugin("a"), _plugin("b", requires=("a",)), _plugin("c", requires=("b",))]
    assert {p.name for p in plugins.select(installed, ["c"], [])} == {"a", "b", "c"}


def test_select_disable_wins_over_a_pulled_in_requirement():
    """An explicit --disable overrides even a dependency the enabled plugin
    asked for -- the same "disable always wins" rule as everything else."""
    installed = [_plugin("odooly"), _plugin("pos", requires=("odooly",))]
    assert [p.name for p in plugins.select(installed, ["pos"], ["odooly"])] == ["pos"]


def test_select_ignores_a_requirement_that_is_not_installed():
    """A plugin naming a dependency that isn't installed still loads --
    it just can't reach what it depends on."""
    installed = [_plugin("pos", requires=("odooly",))]
    assert [p.name for p in plugins.select(installed, ["pos"], [])] == ["pos"]
