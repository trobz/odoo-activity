"""Opt-in, read-only configuration inventory over the existing psql transport.

Mirrors odoo-db's get_modules and allowlisted config parameter queries.
"Installed" includes modules pending an upgrade or uninstall ('to upgrade',
'to remove'): both are still loaded. 'to install' is not installed yet.
CSV keeps the queries compatible with PostgreSQL versions predating JSON.
"""

from __future__ import annotations

from odoo_activity.plugins import Plugin
from odoo_activity.probes import LOCAL, Host, PgTarget, _psql_csv

# Explicit allowlist: never export arbitrary parameters (database.secret,
# API tokens, etc.). Missing keys remain None, with no assumed Odoo defaults.
PARAMETERS = {
    "auth_signup.invitation_scope": "Signup invitation scope",
    "auth_password_policy.minlength": "Minimum password length",
    "auth_totp.policy": "MFA policy (availability depends on installed addons)",
    "web.base.url": "Canonical URL used for generated links",
    "web.base.url.freeze": "Whether automatic base URL updates are frozen",
    "report.url": "Base URL used for report rendering",
    "database.is_neutralized": "Database neutralization marker",
    # Copy-pipeline markers, same keys as odoo-db's `list` flags.
    "database.is_configured_for_safe_testing": "Copy configured for safe testing marker",
    "database.is_shrunk": "Database shrinking marker",
    "database.is_anonymized": "Database anonymization marker",
    "database.is_obfuscated": "Database obfuscation marker",
    "database.source_dump_datetime": "When the source dump of this copy was taken (raw stored value)",
}

_MODULES_SQL = """
SELECT name, latest_version, auto_install, state
FROM ir_module_module
WHERE state IN ('installed', 'to upgrade', 'to remove')
ORDER BY name
"""
_PARAMETERS_SQL = (
    "SELECT key, value FROM ir_config_parameter WHERE key IN ("  # noqa: S608 — constant allowlist only
    + ", ".join(f"'{key}'" for key in PARAMETERS)
    + ") ORDER BY key"
)


def retrieve(database: str, pg: PgTarget, host: Host = LOCAL) -> dict:
    """Return installed modules and stored settings; fail on either query error."""
    rows, error = _psql_csv(_MODULES_SQL, database, {}, pg, host)
    if rows is None:
        raise ValueError(error or "installed module query failed")
    modules = []
    for row in rows:
        if len(row) != 4:
            msg = "installed module query returned an unexpected column count"
            raise ValueError(msg)
        name, version, auto_install, state = row
        modules.append({"name": name, "version": version or "", "auto_install": auto_install == "t", "state": state})

    rows, error = _psql_csv(_PARAMETERS_SQL, database, {}, pg, host)
    if rows is None:
        raise ValueError(error or "config parameter query failed")
    values = {}
    for row in rows:
        if len(row) != 2:
            msg = "config parameter query returned an unexpected column count"
            raise ValueError(msg)
        key, value = row
        if key in PARAMETERS:
            values[key] = value
    return {
        "database": database,
        "modules": modules,
        "config_parameters": [
            {"key": key, "value": values.get(key), "explanation": explanation}
            for key, explanation in PARAMETERS.items()
        ],
    }


class ConfigAuditPlugin(Plugin):
    """MCP-only configuration inventory, enabled explicitly at launch."""

    name = "config-audit"
    default = False
    requires = ()
