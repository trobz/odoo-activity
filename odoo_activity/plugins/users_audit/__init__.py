"""The users-audit plugin: batch export of Odoo user accounts and hashes.

One file per database (`<db>_accounts.json`) carrying the same user picture
`odoo-db users --summarize` gives us, plus the raw account rows:

- `summary.last_login_by_year` / `summary.mfa_by_type`, matching
  `odoo-db users --summarize --summarize-by year`;
- every active user's `id`, `login`, `email`, `profile` and stored password hash.

Consumers: the `password-audit` plugin (which converts the file into a
hashcat target) and an external leaked-account check. `users-audit` is the
single database reader for accounts and hashes — `password-audit` reads no
database.

Opt-in (`--enable-plugins=users-audit`), for the same reason password-audit
is: the export is credential material *and* PII (login, email) under GDPR.
Only enable it on a server an operator has decided may do that.

PostgreSQL compatibility: Odoo 8 can run on PostgreSQL 8.4/9.0, so every
query here is JSON-free. Payloads come back as CSV via
`COPY (SELECT ...) TO STDOUT WITH CSV NULL '\\N'` (PG 8.0+), and schema
probes use `pg_class`/`pg_attribute` joins rather than `to_regclass` (9.4+).
Recursive group traversal requires PostgreSQL 8.4+.
No `json_agg` (9.3+) or `json_build_object` (9.5+) anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from odoo_activity.plugins import Plugin
from odoo_activity.plugins._private import ensure_dir, safe_write
from odoo_activity.probes import LOCAL, Host, PgTarget, _psql_csv

# credential material, same private scratch discipline as password-audit
WORK_DIR = Path("/tmp/oa-users-audit")

_MFA_TYPE_ORDER = ("totp", "passkey", "mail", "none")


def _slug(value: str, fallback: str) -> str:
    """A filesystem-safe path component; never empty, never traversing."""
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "_", value)[:100].strip(".") or fallback
    if slug != value:
        # Sanitization/truncation must not merge distinct databases or hosts.
        slug += "~" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return slug


def run_path(root: Path | None = None) -> Path:
    """A fresh 0700 run directory; never reuse a previous run's files."""
    if root is not None:
        # `output_dir` is an operator-chosen path, so create its parents --
        # ensure_dir() then enforces the 0700/owned-by-us rule.
        Path(root).mkdir(parents=True, mode=0o700, exist_ok=True)
    base = ensure_dir(Path(root) if root is not None else WORK_DIR).absolute()
    return ensure_dir(Path(tempfile.mkdtemp(prefix="run-", dir=base)))


def host_identity(host: Host) -> str:
    """How a host is named inside the run tree and the accounts file."""
    identity = host.alias or "local"
    if host.port is not None:
        identity += f":{host.port}"
    if host.container is not None:
        identity += f"/{host.container}"
    return identity


def accounts_path(run: Path, host_identity_value: str, instance: str, database: str) -> Path:
    folder = ensure_dir(ensure_dir(run / _slug(host_identity_value, "local")) / _slug(instance, "instance"))
    return folder / f"{_slug(database, 'database')}_accounts.json"


# ---------------------------------------------------------------------------
# schema probes (one round trip, catalog only, PG 8-compatible)
# ---------------------------------------------------------------------------

_COLUMN_COUNT = (
    "SELECT count(*) FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
    "WHERE c.relname = 'res_users' AND ns.nspname = 'public' "
    "AND a.attname = '{column}' AND NOT a.attisdropped"
)
_TABLE_COUNT = (
    "SELECT count(*) FROM pg_class c "
    "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
    "WHERE c.relname = '{table}' AND ns.nspname = 'public' AND c.relkind = 'r'"
)

# One row of facts per database. Scalar subqueries keep it a single psql call
# while staying on catalog/SQL features PostgreSQL 8 knows.
_PROBE_SQL = f"""
SELECT
  ({_COLUMN_COUNT.format(column="password_crypt")}) AS has_password_crypt,
  ({_COLUMN_COUNT.format(column="share")}) AS has_share,
  ({_COLUMN_COUNT.format(column="totp_secret")}) AS has_totp,
  ({_TABLE_COUNT.format(table="res_users_log")}) AS has_log,
  ({_TABLE_COUNT.format(table="auth_passkey_key")}) AS has_passkey,
  (SELECT value FROM ir_config_parameter WHERE key = 'auth_totp.policy') AS totp_policy,
  (SELECT latest_version FROM ir_module_module WHERE name = 'base') AS latest_version
"""


def _probe(pg: PgTarget, db: str, host: Host) -> tuple[dict | None, str]:
    """Schema/policy/version facts for one database, or a psql error."""
    rows, error = _psql_csv(_PROBE_SQL, db, {}, pg, host)
    if rows is None:
        return None, error
    if not rows:
        return None, "schema probe returned no row"
    cells = rows[0]
    if len(cells) != 7:
        return None, f"schema probe returned {len(cells)} columns, expected 7"
    has_crypt, has_share, has_totp, has_log, has_passkey, policy, version = cells
    return {
        "legacy": _truthy(has_crypt),
        "share": _truthy(has_share),
        "totp": _truthy(has_totp),
        "log": _truthy(has_log),
        "passkey": _truthy(has_passkey),
        "policy": policy,
        "version": _short_version(version),
    }, ""


def _truthy(cell: str | None) -> bool:
    """A CSV cell from a boolean column ("t"/"f") or a count ("0"/"1")."""
    return cell not in (None, "", "0", "f", "false", "False")


def _short_version(version: str | None) -> str | None:
    """`16.0.1.3` -> `16.0`; anything unparseable is kept as-is."""
    if not version:
        return None
    parts = version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version


# ---------------------------------------------------------------------------
# the account query
# ---------------------------------------------------------------------------


def _accounts_sql(*, legacy: bool, log: bool, totp: bool, passkey: bool, share: bool, include_inactive: bool) -> str:
    """The account query, built from the probe facts.

    Mirrors `odoo-db`'s `get_users` (`db.py:657-769`) and adds the partner
    email, which lives on `res_partner` — `res.users.email` is only a related
    field (`14.0:323`, `16.0:399`, `18.0:409`, `19.0:253`).
    """
    password = (
        "CASE WHEN COALESCE(ru.password, '') <> '' THEN ru.password ELSE ru.password_crypt END"
        if legacy
        else "ru.password"
    )
    if log:
        last_login = "ull.last_login"
        last_login_join = (
            "LEFT JOIN (SELECT create_uid AS uid, MAX(create_date) AS last_login "
            "FROM res_users_log GROUP BY create_uid) ull ON ull.uid = ru.id"
        )
    else:
        # Odoo 8 has no res_users_log; login_date is a real column there.
        last_login = "ru.login_date"
        last_login_join = ""
    totp_expr = "(ru.totp_secret IS NOT NULL AND ru.totp_secret NOT IN ('', 'false'))" if totp else "FALSE"
    if passkey:
        passkey_expr = "COALESCE(pk.n, 0)"
        passkey_join = (
            "LEFT JOIN (SELECT create_uid AS uid, count(*) AS n "
            "FROM auth_passkey_key GROUP BY create_uid) pk ON pk.uid = ru.id"
        )
    else:
        passkey_expr = "0"
        passkey_join = ""
    # res_users.share only exists from 9.0 on; a database without it has no
    # share users to enforce email OTP against.
    share_expr = "ru.share" if share else "FALSE"

    # Odoo 19+ stores only explicit memberships. Traverse implied groups on
    # every version, resolving XML IDs rather than assuming numeric IDs.
    def in_group(name: str) -> str:
        return (
            "EXISTS (SELECT 1 FROM effective_groups gu "
            "JOIN ir_model_data imd ON imd.res_id = gu.gid "
            "WHERE gu.uid = ru.id AND imd.model = 'res.groups' "
            f"AND imd.module = 'base' AND imd.name = '{name}')"
        )

    internal_expr = "NOT ru.share" if share else in_group("group_user")
    profile_expr = (
        f"CASE WHEN {in_group('group_system')} THEN 'admin' WHEN {internal_expr} THEN 'internal' ELSE 'portal' END"
    )
    where = "" if include_inactive else "WHERE ru.active = TRUE"
    # UNION deduplicates memberships and terminates even if groups form a cycle.
    return f"""
WITH RECURSIVE effective_groups(uid, gid) AS (
    SELECT uid, gid FROM res_groups_users_rel
    UNION
    SELECT eg.uid, implied.hid
    FROM effective_groups eg
    JOIN res_groups_implied_rel implied ON implied.gid = eg.gid
)
SELECT ru.id, ru.login, rp.email, {password} AS password,
       {last_login} AS last_login, {totp_expr} AS totp_set,
       {passkey_expr} AS passkey_count, {share_expr} AS share,
       {profile_expr} AS profile
FROM res_users ru
LEFT JOIN res_partner rp ON rp.id = ru.partner_id
{last_login_join}
{passkey_join}
{where}
ORDER BY ru.id
"""


def _accounts(
    pg: PgTarget, db: str, host: Host, *, include_inactive: bool, facts: dict
) -> tuple[list[dict] | None, str]:
    """Run the account query for already-probed `facts` and decode the CSV."""
    sql = _accounts_sql(
        legacy=facts["legacy"],
        log=facts["log"],
        totp=facts["totp"],
        passkey=facts["passkey"],
        share=facts["share"],
        include_inactive=include_inactive,
    )
    rows, error = _psql_csv(sql, db, {}, pg, host)
    if rows is None:
        return None, error
    return _decode_rows(rows, policy=facts["policy"]), ""


def _decode_rows(rows: list[list[str | None]], *, policy: str | None) -> list[dict]:
    out = []
    for row in rows:
        if len(row) != 9:
            msg = f"account query returned {len(row)} columns, expected 9"
            raise ValueError(msg)
        uid, login, email, password, last_login, totp_set, passkey_count, share, profile = row
        out.append({
            "id": int(uid or 0),
            "login": login or "",
            "email": email or "",
            "password": password,
            "profile": profile,
            "last_login": last_login,
            "mfa": compute_mfa(
                totp_set=_truthy(totp_set),
                passkey_count=int(passkey_count or 0),
                share=_truthy(share),
                totp_policy=policy,
            ),
        })
    return out


def compute_mfa(*, totp_set: bool, passkey_count: int, share: bool, totp_policy: str | None) -> str:
    """Comma-joined set of a user's active MFA methods, or ``"none"``.

    Ported verbatim from `odoo-db` (`db.py:619`) so the two tools' summaries
    agree: ``totp`` and ``passkey`` are factors the user owns, ``mail`` is
    the enterprise email-OTP policy applying to this user (suppressed by
    ``totp``, not by ``passkey``).
    """
    methods: list[str] = []
    if totp_set:
        methods.append("totp")
    if passkey_count > 0:
        methods.append("passkey")
    enforced = totp_policy == "all_required" or (totp_policy == "employee_required" and not share)
    if enforced and not totp_set:
        methods.append("mail")
    return ", ".join(methods) if methods else "none"


def summarize_users(rows: list[dict], by: str = "year") -> dict:
    """Aggregate account rows exactly like `odoo-db`'s `summarize_users`.

    ``by`` is ``"year"`` (default here) or ``"month"``; the key is named for
    the bucketing. ``never`` holds accounts that never logged in and sorts
    last. MFA factors are counted independently (a ``totp, passkey`` user
    counts in both), so the mfa counts can exceed the user total.
    """
    width = 4 if by == "year" else 7  # "YYYY" vs "YYYY-MM"
    buckets: dict[str, int] = {}
    mfa_counts: dict[str, int] = {}
    for row in rows:
        last_login = row.get("last_login")
        # Only date buckets belong in the public manifest, never arbitrary
        # database text (including malformed values containing account data).
        bucket = last_login[:width] if last_login and re.match(r"^\d{4}-\d{2}-\d{2}", last_login) else "never"
        buckets[bucket] = buckets.get(bucket, 0) + 1
        for token in (row.get("mfa") or "none").split(", "):
            mfa_counts[token] = mfa_counts.get(token, 0) + 1

    ordered = sorted(bucket for bucket in buckets if bucket != "never")
    last_login_counts = {bucket: buckets[bucket] for bucket in ordered}
    if "never" in buckets:
        last_login_counts["never"] = buckets["never"]

    mfa_by_type = {token: mfa_counts[token] for token in _MFA_TYPE_ORDER if token in mfa_counts}
    # Any unexpected token (custom _mfa_type) keeps a stable, visible slot.
    for token in mfa_counts:
        if token not in mfa_by_type:
            mfa_by_type[token] = mfa_counts[token]

    return {f"last_login_by_{by}": last_login_counts, "mfa_by_type": mfa_by_type}


def _companies(pg: PgTarget, db: str, host: Host) -> tuple[list[str], str]:
    rows, error = _psql_csv("SELECT name FROM res_company ORDER BY id", db, {}, pg, host)
    if rows is None:
        return [], error
    return [row[0] for row in rows if row and row[0]], ""


def build_payload(
    *,
    database: str,
    instance: str,
    host_identity_value: str,
    rows: list[dict],
    companies: list[str],
    odoo_version: str | None,
    include_inactive: bool,
    exported_at: datetime | None = None,
) -> dict:
    """The `<db>_accounts.json` document.

    Per-user rows keep account fields and the access profile; the summary carries the
    last-login/MFA aggregates the rows were computed from.
    """
    moment = exported_at or datetime.now(timezone.utc)
    return {
        "database": database,
        "instance": instance,
        "host": host_identity_value,
        "exported_at": moment.isoformat(timespec="seconds"),
        "odoo_version": odoo_version,
        "include_inactive": include_inactive,
        "companies": companies,
        "summary": summarize_users(rows, by="year"),
        "users": [
            {
                "id": row["id"],
                "login": row["login"],
                "email": row["email"],
                "password": row["password"],
                "profile": row["profile"],
            }
            for row in rows
        ],
    }


def export_database(
    *,
    instance: str,
    database: str,
    pg: PgTarget,
    host: Host = LOCAL,
    host_identity_value: str = "local",
    include_inactive: bool = False,
    run: Path | None = None,
) -> tuple[dict | None, str]:
    """Export one database into the run tree; `(manifest entry, error)`."""
    run_dir = run if run is not None else run_path()
    facts, error = _probe(pg, database, host)
    if facts is None:
        return None, error
    rows, error = _accounts(pg, database, host, include_inactive=include_inactive, facts=facts)
    if rows is None:
        return None, error
    companies, _company_error = _companies(pg, database, host)
    payload = build_payload(
        database=database,
        instance=instance,
        host_identity_value=host_identity_value,
        rows=rows,
        companies=companies,
        odoo_version=facts["version"],
        include_inactive=include_inactive,
    )
    path = accounts_path(run_dir, host_identity_value, instance, database)
    try:
        safe_write(path, json.dumps(payload, indent=2))
    except OSError as exc:
        return None, f"{path}: {exc}"
    return {
        "host": host_identity_value,
        "instance": instance,
        "database": database,
        "path": str(path),
        "users": len(payload["users"]),
        "summary": payload["summary"],
        "error": None,
    }, ""


# ---------------------------------------------------------------------------
# the Plugin class
# ---------------------------------------------------------------------------


class UsersAuditPlugin(Plugin):
    """Contributes the MCP tools; no TUI tab (this is agent/operator work)."""

    name = "users-audit"
    default = False  # opt-in: credential material plus PII
    requires = ()  # talks to postgres directly, not through odooly
