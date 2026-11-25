"""Opt-in, read-only configuration inventory over the existing psql transport.

Mirrors odoo-db's get_modules and allowlisted config parameter queries.
"Installed" includes modules pending an upgrade or uninstall ('to upgrade',
'to remove'): both are still loaded. 'to install' is not installed yet.
CSV keeps the queries compatible with PostgreSQL versions predating JSON.

OCA `password_security` keeps its policy in `ir_config_parameter` from 17.0
(`password_security.*`, in the allowlist) and on `res_company` before (one
policy per company, columns varying by version): when the module is
installed, those columns are read too, whichever of them exist.

When `website` is installed (Odoo 12.0+), `website.auth_signup_uninvited`
overrides `auth_signup.invitation_scope` per website: each website's signup
scope is read from `website` when that column exists.
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
    # OCA password_security, 17.0+ (earlier versions: per company, see PASSWORD_SECURITY_COLUMNS).
    "password_security.expiration_days": "password_security: days before a password expires (0: never)",
    "password_security.minimum_hours": "password_security: hours before a password can be changed again",
    "password_security.history": "password_security: previous passwords that cannot be reused (-1: all)",
    "password_security.lower": "password_security: minimum lowercase letters",
    "password_security.upper": "password_security: minimum uppercase letters",
    "password_security.numeric": "password_security: minimum digits",
    "password_security.special": "password_security: minimum special characters",
}

# OCA password_security before 17.0: res_company columns (password_policy_enabled
# from 15.0; password_length and password_estimate dropped in 16.0 for
# auth_password_policy.minlength). Only the ones present are read.
PASSWORD_SECURITY_COLUMNS = {
    "password_policy_enabled": "Policy enabled for the company (15.0; off: no rule enforced)",
    "password_length": "Minimum password length (up to 15.0)",
    "password_lower": "Minimum lowercase letters",
    "password_upper": "Minimum uppercase letters",
    "password_numeric": "Minimum digits",
    "password_special": "Minimum special characters",
    "password_estimate": "Minimum zxcvbn strength score, 0-4 (up to 15.0)",
    "password_expiration": "Days before a password expires (0: never)",
    "password_minimum": "Hours before a password can be changed again",
    "password_history": "Previous passwords that cannot be reused (-1: all)",
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

_COMPANY_COLUMNS_SQL = (
    # Resolve the same relation as the unqualified SELECT below, even when
    # res_company lives later in search_path than current_schema().
    "SELECT attname FROM pg_catalog.pg_attribute"  # noqa: S608 — constant allowlist only
    " WHERE attrelid = 'res_company'::regclass AND attnum > 0 AND NOT attisdropped AND attname IN ("
    + ", ".join(f"'{column}'" for column in PASSWORD_SECURITY_COLUMNS)
    + ")"
)

WEBSITE_COLUMNS = ("domain", "auth_signup_uninvited")

_WEBSITE_COLUMNS_SQL = (
    "SELECT attname FROM pg_catalog.pg_attribute"  # noqa: S608 — constant allowlist only
    " WHERE attrelid = 'website'::regclass AND attnum > 0 AND NOT attisdropped AND attname IN ("
    + ", ".join(f"'{column}'" for column in WEBSITE_COLUMNS)
    + ")"
)


def _cell(value: str | None) -> bool | int | str | None:
    """psql CSV cell -> bool ('t'/'f'), int, or the raw string; NULL stays None."""
    if value in ("t", "f"):
        return value == "t"
    try:
        return int(value) if value is not None else None
    except ValueError:
        return value


def _password_security_companies(database: str, pg: PgTarget, host: Host) -> list[dict]:
    """Per-company password_security policy (before 17.0); [] when it keeps none on res_company."""
    rows, error = _psql_csv(_COMPANY_COLUMNS_SQL, database, {}, pg, host)
    if rows is None:
        raise ValueError(error or "res_company column query failed")
    present = {row[0] for row in rows if row}
    columns = [column for column in PASSWORD_SECURITY_COLUMNS if column in present]  # allowlist order
    if not columns:
        return []
    sql = f"SELECT id, name, {', '.join(columns)} FROM res_company ORDER BY id"  # noqa: S608 — allowlisted names
    rows, error = _psql_csv(sql, database, {}, pg, host)
    if rows is None:
        raise ValueError(error or "res_company policy query failed")
    companies = []
    for row in rows:
        if len(row) != len(columns) + 2:
            msg = "res_company policy query returned an unexpected column count"
            raise ValueError(msg)
        companies.append({
            "id": _cell(row[0]),
            "name": row[1],
            **{c: _cell(v) for c, v in zip(columns, row[2:], strict=True)},
        })
    return companies


def _websites(database: str, pg: PgTarget, host: Host) -> list[dict]:
    """Per-website signup scope (Odoo 12.0+); [] when website has no auth_signup_uninvited column."""
    rows, error = _psql_csv(_WEBSITE_COLUMNS_SQL, database, {}, pg, host)
    if rows is None:
        raise ValueError(error or "website column query failed")
    present = {row[0] for row in rows if row}
    if "auth_signup_uninvited" not in present:
        return []
    columns = [column for column in WEBSITE_COLUMNS if column in present]
    sql = f"SELECT id, name, {', '.join(columns)} FROM website ORDER BY id"  # noqa: S608 — allowlisted names
    rows, error = _psql_csv(sql, database, {}, pg, host)
    if rows is None:
        raise ValueError(error or "website query failed")
    websites = []
    for row in rows:
        if len(row) != len(columns) + 2:
            msg = "website query returned an unexpected column count"
            raise ValueError(msg)
        websites.append({
            "id": _cell(row[0]),
            "name": row[1],
            **dict(zip(columns, row[2:], strict=True)),
        })
    return websites


def retrieve(database: str, pg: PgTarget, host: Host = LOCAL) -> dict:
    """Return installed modules, stored settings and, when installed, OCA
    password_security's per-company policy and per-website signup scopes; fail
    on any query error."""
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
    installed = {module["name"] for module in modules}
    password_security = None
    if "password_security" in installed:
        companies = _password_security_companies(database, pg, host)
        password_security = {
            "companies": companies,
            "explanations": {c: e for c, e in PASSWORD_SECURITY_COLUMNS.items() if any(c in co for co in companies)},
        }
    websites = _websites(database, pg, host) if "website" in installed else None
    return {
        "database": database,
        "modules": modules,
        "config_parameters": [
            {"key": key, "value": values.get(key), "explanation": explanation}
            for key, explanation in PARAMETERS.items()
        ],
        # None: module not installed; companies == [] on 17.0+ (policy in config_parameters)
        "password_security": password_security,
        # None: website not installed; [] when website has no auth_signup_uninvited column (<= 11.0)
        "websites": websites,
    }


class ConfigAuditPlugin(Plugin):
    """MCP-only configuration inventory, enabled explicitly at launch."""

    name = "config-audit"
    default = False
    requires = ()
