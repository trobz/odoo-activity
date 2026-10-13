"""The password-audit plugin: MCP tools to pull password hashes off Odoo
databases and feed them to hashcat on this machine's GPU.

Opt-in (`--enable-plugins=password-audit`), like pos. This is deliberately
NOT a default plugin: it bulk-exports credential material (user password
hashes, personal data under GDPR) over the existing ssh/probe paths, and it
shells out to hashcat. It should only be enabled on a server an operator
has decided is allowed to do that — our own company databases, per
docs/plans/todo/password-strength-testing.md.

Design notes (why it looks like this):

- **Derived wordlist first.** `password_hashes` also writes a companion
  wordlist of the login/company-derived guesses (login itself, its email
  local part, `{Company}{year}!`-style mutations built from the database's
  res.company names and the years around now) — the guesses that beat any
  generic corpus on a company database, tried before a big rockyou run.

- **`password_hashes` answers one flat payload** — never per-user rows in
  a conversation, so the hashes land in a file the operator feeds to
  hashcat, not in the chat log. In multi-host mode the file aggregates
  every reachable host.

- **hashcat runs locally** on this machine's GPU (`password_crack`), with
  mode 12100 (`PBKDF2-HMAC-SHA512`, the passlib scheme Odoo 16+ stores).
  The plugin converts passlib's ab64 encoding to hashcat's expected
  `sha512:rounds:b64salt:b64digest` format — the `+`→`.` detail is where a
  naive converter silently produces zero cracks (same pitfall odoo-db's
  parser hit, see its positive-control test).
"""

from __future__ import annotations

import base64
import json
import re
import string
import subprocess
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from odoo_activity.plugins import Plugin
from odoo_activity.probes import LOCAL, Host, PgTarget, _psql_json
from odoo_activity.probes import pg_target_of as pg_target_of  # re-exported for mcp_server

# where hash material and wordlists live. Scratch by default: a tmpfs-backed
# workspace the OS clears, so the hashes do not outlive the audit.
WORK_DIR = Path("/tmp/oa-password-audit")


def _ensure_dir(d: Path) -> Path:
    """A credential-bearing dir, 0700 and owned by us — other local users
    must not be able to read (or pre-create) it: a pre-created dir or a
    symlinked file could redirect the material."""
    import os

    d.mkdir(mode=0o700, exist_ok=True)
    if d.is_symlink():
        msg = f"{d}: symlinked audit directory is not allowed"
        raise PermissionError(msg)
    st = d.stat()
    if st.st_uid != os.getuid() or (st.st_mode & 0o777) != 0o700:
        msg = f"{d} exists with uid {st.st_uid} mode {oct(st.st_mode & 0o777)} — expected 0700 owned by us"
        raise PermissionError(msg)
    return d


def export_path(instance: str, db: str, host_identity: str = "local") -> Path:
    """Reserve a fresh 0700 audit namespace; never reuse prior credential files.

    Call once per export and pass the returned destination to
    write_hashcat_file. Host identity is part of the namespace, not just
    a display label. Delete the entire audit directory after the audit.
    """
    import hashlib
    import tempfile

    root = _ensure_dir(WORK_DIR)
    identity = json.dumps([instance, db, host_identity], ensure_ascii=True)
    scope = _ensure_dir(root / hashlib.sha256(identity.encode()).hexdigest())
    audit = Path(tempfile.mkdtemp(prefix="audit-", dir=scope))
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{instance}_{db}")[:100] or "export"
    return audit / f"{name}.hashcat"


def _safe_write(path: Path, content: str | bytes) -> None:
    """0600 without following symlinks: O_CREAT|O_EXCL fails if a hostile
    file/symlink already sits at the path (predictable names in /tmp)."""
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content.encode("utf-8") if isinstance(content, str) else content)


# hashcat mode for passlib's pbkdf2-sha512
HASHCAT_MODE = 12100

# The fixed part of the derived wordlist: common weak passwords. The
# mutation templates take {company} (from the database's res.company —
# "Company2024!" is only interesting if it is the *right* company) and
# logins (from the export itself).
# Superset of odoo-db check-passwords' common list (which is: admin, odoo,
# admin123, odoo123, password, 123456, 1234567890, azerty, qwerty, demo) —
# the derived wordlist must cover at least everything that scan catches.
# Year variants (admin2026, Odoo2025!) are generated from the live clock by
# _common_year_variants, not hard-coded here.
COMMON_WEAK: tuple[str, ...] = (
    "admin",
    "odoo",
    "password",
    "123456",
    "1234567890",
    "azerty",
    "qwerty",
    "welcome",
    "changeme",
    "tochange",
    "demo",
    "portal",
    "test",
    "admin123",
    "odoo123",
    "password123",
    "P@ssw0rd",
)

# The trivial-guess baseline, beyond check-passwords': every 1-char and
# 2-char combination over digits + upper + lowercase — a GPU run absorbs
# what the in-place CPU scan deliberately skipped (uppercase costs
# nothing on a GPU; 2-char is 62² = 3844 candidates, still seconds at
# 600k rounds).
_TRIVIAL_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase

# two tiers, ordered by likelihood-per-second: tier 1 (cheap, high hit
# rate) = the 62 single characters, tried together with the common list,
# logins and company mutations; tier 2 (3844 2-char combos, the slow tail)
# is a separate wordlist so an attack can stop after tier 1 when the
# budget is better spent there.
_TIER1_CANDIDATES = tuple(_TRIVIAL_ALPHABET)
_TIER2_CANDIDATES = tuple(a + b for a in _TRIVIAL_ALPHABET for b in _TRIVIAL_ALPHABET)
# hashcat mask covering the same 62² candidates (used by calibrate() — a
# mask attack avoids materializing the wordlist for the benchmark)
_TIER2_MASK = "?1?1"

# mutation templates: {company} is the company name (case-variants), {year}
# the current and previous years, {season} the seasons around now. The point
# is "Acme2024!" beats rockyou for a company database.
_MUTATION_TEMPLATES = (
    "{company}{year}",
    "{company}{year}!",
    "{company}{year}?",
    "{company}-{year}",
    "{year}{company}",
    "{company}",
    "{season}{year}!",
    "{season}{year}",
    "{company}{season}",
)

_SEASONS = ("spring", "summer", "autumn", "winter", "Spring", "Summer", "Autumn", "Winter")


def _years() -> list[str]:
    """The current and the two previous years, from the live clock — a
    password audit run next year should try next year's numbers."""
    import datetime

    year = datetime.date.today().year
    return [str(year - offset) for offset in (0, 1, 2)]


# the default-credential words most often "upgraded" with a year
# (admin2026, Odoo2026!) — tier-1, unlike company/season year mutations,
# because they need no per-db input and are a handful of candidates
_YEAR_BASES = ("admin", "odoo")
_YEAR_SUFFIXES = ("", "!")


def _common_year_variants(years: list[str]) -> list[str]:
    """admin/odoo + year, lowercase and capitalized, bare or with a
    trailing '!': 2 bases x 2 cases x len(years) x 2 suffixes."""
    return [
        f"{case}{year}{suffix}"
        for base in _YEAR_BASES
        for case in (base, base.capitalize())
        for year in years
        for suffix in _YEAR_SUFFIXES
    ]


# trailing punctuation worth guessing after a company name (tier-1);
# year/season combos stay in tier-2
_SYMBOLS = ("!", "#", "$", "*", "?", ".", "-", "_")


def _company_variants(companies: list[str]) -> list[str]:
    """Case variants worth guessing, for the FULL name (with the no-space
    squeeze: 'Acme Corp' -> 'AcmeCorp', 'acmecorp') AND for each word
    alone ('Trobz Consulting' -> 'Trobz', 'Consulting') — users pick the
    distinctive word, not necessarily the full name."""
    variants: list[str] = []
    for name in companies:
        # Line-oriented wordlists cannot preserve record separators. Normalize
        # them before deriving full names, squeezes, words, leet or templates.
        # CRLF is one boundary, matching str.splitlines().
        name = re.sub(r"\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]", " ", name)
        words = [w for w in name.split() if w]
        bases = [name, name.replace(" ", ""), *words]
        for base in bases:
            for cand in (base, base.lower(), base.upper(), base.title()):
                if cand and cand not in variants:
                    variants.append(cand)
    return variants


def build_candidates(companies: list[str] | None = None, logins: list[str] | None = None) -> list[str]:
    """Tier-1 wordlist, LEAN for minimum coverage: common weak passwords,
    admin/odoo + recent-year variants (live clock), login-derived guesses
    (full login + email local part), the bare company name (each case
    variant), and the 62 single characters — at
    least the baseline odoo-db's check-passwords tests, deduped,
    order-stable. Leet additions use o0/e3/a4/i1, one type replacing all
    occurrences, bare or ! suffix only; at most 128 new candidates per
    unique company name (original company/symbol guesses are never capped).
    The company/season/year MUTATION variants live in
    tier-2 (`build_tier2`): they doubled this list to ~2.5k candidates
    per db, defeating "minimum coverage fits any budget".
    """
    candidates = [*COMMON_WEAK, *_common_year_variants(_years())]
    for login in logins or []:
        for guess in (login, login.split("@")[0]):  # full login AND email local part
            # A line-oriented wordlist cannot represent record separators.
            # Skip each unsafe guess, retaining a valid local part even when
            # the domain is multiline. Never split it into injected guesses.
            if any(separator in guess for separator in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
                continue
            if guess and guess not in candidates:
                candidates.append(guess)
    candidates.extend(c for c in _TIER1_CANDIDATES if c not in candidates)
    # bare company name plus trailing-punctuation variants (Lacaravane!,
    # Superquinquin#): a symbol suffix is a common real-world tweak and
    # neither tier-2's mutations ({company}{year}-shaped) nor the bare name
    # covers it. 8 symbols x ~8 company variants ≈ 60-80 extra candidates.
    for cand in _company_variants(companies or []):
        candidates.append(cand)
        for symbol in _SYMBOLS:
            candidates.append(cand + symbol)
    # One substitution type applied to all occurrences, bare or ! only.
    for name in dict.fromkeys(companies or []):
        guesses = (
            "".join(digit if ch.lower() == letter else ch for ch in base)
            for base in _company_variants([name])
            for letter, digit in (("o", "0"), ("e", "3"), ("a", "4"), ("i", "1"))
            if letter in base.lower()
        )
        _append_bounded(candidates, (cand for guess in guesses for cand in (guess, guess + "!")), 128)
    return list(dict.fromkeys(candidates))


def _append_bounded(candidates: list[str], guesses: Iterable[str], limit: int) -> None:
    """Append at most limit new unique guesses, stopping the input early."""
    seen = set(candidates)
    added = 0
    for guess in guesses:
        if guess not in seen:
            candidates.append(guess)
            seen.add(guess)
            added += 1
            if added == limit:
                break


_LEET_OPTIONS = {
    "o": ("0",),
    "e": ("3",),
    "a": ("4", "@"),
    "i": ("1", "!"),
    "s": ("5", "$"),
    "t": ("7",),
    "l": ("1",),
    "b": ("8",),
    "g": ("9",),
    "z": ("2",),
}


def _leet_variants(base: str) -> list[str]:
    """Up to 128 unique changed forms per case-sensitive base.

    Prioritize each all-occurrence replacement, then cumulative first-choice
    replacements in o/e/a/i/s/t/l/b/g/z order, then lazy per-character
    Cartesian options (unchanged first). Never materialize the full product.
    """
    import itertools

    variants: dict[str, None] = {}
    cumulative = base
    for letter, replacements in _LEET_OPTIONS.items():
        for replacement in replacements:
            guess = "".join(replacement if ch.lower() == letter else ch for ch in base)
            if guess != base:
                variants[guess] = None
        cumulative = "".join(replacements[0] if ch.lower() == letter else ch for ch in cumulative)
        if cumulative != base:
            variants[cumulative] = None
    options = [(ch, *_LEET_OPTIONS.get(ch.lower(), ())) for ch in base]
    for parts in itertools.product(*options):
        guess = "".join(parts)
        if guess != base:
            variants[guess] = None
        if len(variants) >= 128:
            break
    return list(variants)


def _company_templates(company: str, years: list[str]) -> Iterator[str]:
    """Yield the unchanged year/season template corpus for one base."""
    for year in years:
        for template in _MUTATION_TEMPLATES:
            for season in _SEASONS if "{season}" in template else ("",):
                yield template.format(company=company, year=year, season=season)


def _tier2_leet_guesses(name: str, years: list[str]) -> Iterator[str]:
    """Round-robin bases, expanding each leet form before taking the next."""
    import itertools

    bases = [_leet_variants(base) for base in _company_variants([name])]
    for variants in itertools.zip_longest(*bases):
        for company in variants:
            if company is not None:
                yield company
                yield from (company + symbol for symbol in _SYMBOLS)
                yield from _company_templates(company, years)


def build_tier2(companies: list[str] | None = None, logins: list[str] | None = None) -> list[str]:
    """Tier-2 wordlist: the 3,844 two-character combinations PLUS the
    company/season/year mutation variants — the slow tail, run as its own
    `password_crack` call when the plan's budget allows after tier-1
    coverage. Mutations apply to the full name, its no-space squeeze
    ('TrobzConsulting') and each word alone ('Trobz2026!',
    'Consulting2026!') via _company_variants. Leet forms add all 8 symbol
    suffixes and the same templates: at most 128 changed forms per
    case-sensitive base and 8192 new candidates per unique company name,
    including suffix/template expansions. Bases are consumed round-robin.
    These limits NEVER remove original candidates; logins and the trivial
    alphabet are not mutated."""
    candidates = list(_TIER2_CANDIDATES)
    companies = companies or []
    years = _years()
    for company in _company_variants(companies):
        candidates.extend(_company_templates(company, years))
    # Preserve the complete original corpus before capping leet additions.
    for name in dict.fromkeys(companies):
        _append_bounded(candidates, _tier2_leet_guesses(name, years), 8192)
    return list(dict.fromkeys(candidates))


def tier2_candidates() -> list[str]:
    """Tier-2 wordlist (generic part): the 3,844 two-character
    combinations only — used by the planner's sizing; per-db tier-2 files
    are written with mutations by `write_hashcat_file` (build_tier2)."""
    return list(_TIER2_CANDIDATES)


# ---------------------------------------------------------------------------
# hashcat pipeline
# ---------------------------------------------------------------------------


def fetch_hash_rows(target: PgTarget, db: str, host: Host = LOCAL) -> tuple[list[dict] | None, str]:
    """Export active users, detecting legacy storage from the table schema.

    Empty/unconvertible modern results are valid, not evidence of a legacy
    column. On legacy schemas select password or password_crypt per row,
    including mixed databases. Preserve schema and row-query errors.
    """
    columns, error = _psql_json(_HASH_COLUMNS_SQL, db, {}, target, host)
    if columns is None:
        return None, error
    legacy = any(column.get("attname") == "password_crypt" for column in columns)
    return _psql_json(_HASH_SQL_LEGACY if legacy else _HASH_SQL, db, {}, target, host)


_HASH_COLUMNS_SQL = """
SELECT json_agg(t) FROM (
    SELECT attname FROM pg_attribute
    WHERE attrelid = 'res_users'::regclass
      AND attname = 'password_crypt' AND NOT attisdropped
) t
"""


_HASH_SQL = """
SELECT json_agg(t) FROM (
    SELECT id, login, password, COALESCE(share, false) AS share
    FROM res_users
    WHERE active AND password IS NOT NULL
    ORDER BY id
) t
"""


# Odoo < 10 (and some odd cases) store the passlib hash in password_crypt
# with res_users.password empty — exports on those DBs must read the
# crypt column or every user looks "unexportable".
_HASH_SQL_LEGACY = """
SELECT json_agg(t) FROM (
    SELECT id, login,
           CASE WHEN COALESCE(password, '') <> '' THEN password ELSE password_crypt END AS password,
           COALESCE(share, false) AS share
    FROM res_users
    WHERE active
      AND (COALESCE(password, '') <> '' OR COALESCE(password_crypt, '') <> '')
    ORDER BY id
) t
"""


def fetch_company_names(target: PgTarget, db: str, host: Host = LOCAL) -> tuple[list[str] | None, str]:
    """The res.company names, feeding the mutation guesses. `None` rows
    (query failed) leave the company mutations out rather than guessing."""
    # bare json_agg: the column value IS the array — _psql_json returns the
    # list of names directly (no subselect wrapper like fetch_hash_rows).
    rows, error = _psql_json("SELECT json_agg(name) FROM res_company", db, {}, target, host)
    if rows is None:
        return None, error

    # type: _psql_json's declared rows are dicts, but a bare json_agg column
    # is an array of scalars — keep only the string values (defensive too).
    return [name for name in rows if isinstance(name, str) and name], ""


def read_hash_work_groups(path: Path) -> dict[tuple[int, str], int]:
    """Unique digest counts per (rounds, salt), excluding duplicate user rows."""
    groups: dict[tuple[int, str], set[str]] = {}
    for line in path.read_text().splitlines():
        fields = line.strip().split(":")
        if len(fields) != 5 or fields[1] != "sha512" or not fields[3] or not fields[4]:
            continue
        try:
            rounds = int(fields[2])
        except ValueError:
            continue
        if rounds > 0:
            groups.setdefault((rounds, fields[3]), set()).add(fields[4])
    return {group: len(digests) for group, digests in groups.items()}


def read_hash_file_stats(path: Path) -> tuple[int, int, int]:
    """(hash count, max round count, unique salt count) from a written
    hash file — the plan's sizing inputs, no db access needed at planning
    time. The salt count matters: low-salt DBs are latency-bound (hashcat
    can't fill the GPU with few parallel salts) and disproportionately
    expensive per combo."""
    max_rounds = 0
    count = 0
    salts = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        fields = line.split(":")
        # uid=login:sha512:rounds:salt:digest
        if len(fields) >= 4:
            try:
                rounds = int(fields[2])
            except ValueError:
                continue
            count += 1
            salts.add(fields[3])
            max_rounds = max(max_rounds, rounds)
    return count, max_rounds, len(salts)


def _parse_ab64(stored: str) -> tuple[str, int, bytes, bytes]:
    """passlib ab64 (`$pbkdf2-sha512$rounds$salt$digest`, `+`→`.`) to parts."""
    if not stored.startswith("$"):
        msg = "not a passlib hash"
        raise ValueError(msg)
    parts = stored.split("$")
    if len(parts) != 5:
        msg = "unexpected passlib hash shape"
        raise ValueError(msg)

    scheme, rounds_s, salt_s, digest_s = parts[1:]
    if scheme not in ("pbkdf2-sha512", "pbkdf2"):
        msg = "unsupported scheme"
        raise ValueError(msg)

    def ab64(s: str) -> bytes:
        return base64.b64decode(s.replace(".", "+") + "=" * (-len(s) % 4))

    try:
        rounds = int(rounds_s)
    except ValueError:
        msg = "non-numeric round count"
        raise ValueError(msg) from None

    digest = ab64(digest_s)
    expected = 64 if scheme == "pbkdf2-sha512" else 20
    if rounds <= 0 or len(digest) != expected:
        msg = "digest length or round count does not match the scheme"
        raise ValueError(msg)
    return scheme, rounds, ab64(salt_s), digest


def ab64_to_b64(s: str) -> str:
    """passlib ab64 -> standard base64, what hashcat mode 12100 wants."""
    raw = base64.b64decode(s.replace(".", "+") + "=" * (-len(s) % 4))
    return base64.b64encode(raw).decode()


def to_hashcat_line(label: str, stored: str) -> str | None:
    """One `label:sha512:rounds:b64salt:b64digest` line, or None for anything
    hashcat can't take (plaintext, unsupported scheme, odd digest length).

    `label` is anything hashcat's --username keeps as-is before the first
    colon; the plugin passes `uid=login` so results map straight back to
    users without a psql query."""
    try:
        scheme, rounds, _salt, _digest = _parse_ab64(stored)
    except ValueError:
        return None
    if scheme != "pbkdf2-sha512":
        return None
    parts = stored.strip("$").split("$")
    return f"{label}:sha512:{rounds}:{ab64_to_b64(parts[2])}:{ab64_to_b64(parts[3])}"


def write_hashcat_file(rows: list[dict], path: Path, companies: list[str] | None = None) -> int:
    """Write hashcat-format lines for every convertible hash; returns how many.

    The label carries the uid and login (`13=john`), so hashcat's --username
    output maps straight back to the user; the file is credential material
    all the same.

    Next to the hash file, two companion wordlists:
    `<db>.derived.txt` (tier 1 — login/company-derived guesses, the common
    list, single chars; try it first, it is cheap and beats generic
    corpora on company databases) and `<db>.tier2.txt` (tier 2 — the 3844
    two-character combinations, the slow tail; its own call when the
    derived pass left budget to spare).
    """
    # validate the target dir (0700, ours) — path's parent, which is
    # WORK_DIR in production and tmp_path in tests
    _ensure_dir(path.parent)
    lines = []
    from urllib.parse import quote

    # Preserve ordinary ASCII labels, but encode delimiters, controls and
    # Unicode as UTF-8 percent escapes (including every splitlines separator).
    label_safe = string.ascii_letters + string.digits + string.punctuation.replace(":", "").replace("%", "") + " \t"
    login_map: list[tuple[int, str]] = []
    for row in rows:
        login = str(row.get("login", ""))
        safe_login = quote(login, safe=label_safe)
        line = to_hashcat_line(f"{row['id']}={safe_login}", row["password"])
        if line:
            lines.append(line)
            login_map.append((row["id"], login))
    _safe_write(path, "\n".join(lines) + "\n")
    # side file: escaped label -> original login (for uid=login reporting)
    login_map_path = path.with_suffix(".loginmap.json")
    _safe_write(login_map_path, json.dumps(dict(login_map)))

    logins = [orig for _uid, orig in login_map]
    candidates = build_candidates(companies or [], logins)
    _safe_write(path.with_suffix(".derived.txt"), "\n".join(candidates) + "\n")
    # tier-2 carries the mutation variants (moved down from tier-1 — they
    # doubled tier-1 beyond any sane minimum-coverage budget)
    _safe_write(path.with_suffix(".tier2.txt"), "\n".join(build_tier2(companies or [], logins)) + "\n")
    return len(lines)


def _decode_label(label: str) -> str:
    """Undo the export's percent-encoding of the login part of a
    `uid=login` label ('john%3Ahr' -> 'john:hr'). Idempotent for logins
    without escapes."""
    uid, sep, login = label.partition("=")
    if not sep:
        return label
    from urllib.parse import unquote

    return f"{uid}={unquote(login)}"


def _relabel(show: str, hash_file: Path) -> str:
    """Normalize raw and already-labeled records exactly once.

    Join by full hash (including digest), preserving colon-bearing plains
    and every user sharing a copied hash. Keep escaped labels machine-safe;
    password_findings supplies decoded original logins and plaintext bytes.
    """
    labels = {}
    for line in hash_file.read_text().splitlines():
        label, _, rest = line.partition(":")
        parts = rest.split(":")[:4]  # sha512, rounds, salt, digest
        if len(parts) == 4:
            # include the digest: two users sharing a copied hash would
            # otherwise overwrite each other's label on the salt key
            labels.setdefault("sha512:" + ":".join(parts[1:4]), []).append(label)
    relabeled = []
    for show_line in show.splitlines():
        if ":sha512:" in show_line and not show_line.startswith("sha512:"):
            relabeled.append(show_line)
            continue
        prefix = ":".join(show_line.split(":")[:4])
        for label in labels.get(prefix, ["?"]):
            relabeled.append(f"{label}:{show_line}")
    return "\n".join(relabeled)


# Bounded audit work, not exhaustive password search. All wordlist attacks
# are supervised asynchronous jobs, including this default 250-second pass.
RUNTIME_BUDGET_S = 250

# Compatibility constant only: attacks no longer block below this threshold.
SYNC_CALL_LIMIT_S = 260


def _status_path(hash_file: Path) -> Path:
    return hash_file.with_suffix(".status.json")


def _potfile_path(hash_file: Path) -> Path:
    """Credential recovery is confined to this audit, never the global potfile."""
    return hash_file.with_suffix(".potfile")


def _audit_lock(hash_file: Path) -> int:
    """Nonblocking persistent flock; never unlink the inode on completion."""
    import fcntl
    import os

    _ensure_dir(hash_file.parent)
    fd = os.open(hash_file.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(fd)
        raise
    return fd


def _clear_out_file(out_file: Path) -> None:
    import os

    from .worker import private_open

    fd = private_open(out_file, os.O_WRONLY | os.O_CREAT)
    try:
        os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def _runtime(value) -> int:
    import math

    if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        msg = "runtime_budget must be finite and positive"
        raise ValueError(msg)
    return math.ceil(float(value))


# Per-input snapshot byte budget: a hostile or accidental huge regular file
# must fail fast instead of ballooning the audit workspace.
_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


def _validated_input_bytes(path: Path) -> bytes:
    """Read an attack input through an already-validated descriptor.

    private_open opens O_NONBLOCK|O_NOFOLLOW and rejects anything that is
    not a regular, owned, single-link 0600 file — so a FIFO (or device)
    input can never block the launcher on open or read.
    """
    import os

    from .worker import private_open

    try:
        fd = private_open(path)
    except PermissionError as exc:
        msg = (
            f"{path}: attack input must be a regular 0600 single-link file"
            f" owned by us (FIFO/device/symlink rejected): {exc}"
        )
        raise ValueError(msg) from exc
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_SNAPSHOT_BYTES:
                msg = f"{path}: exceeds the {_MAX_SNAPSHOT_BYTES}-byte snapshot budget"
                raise ValueError(msg)
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _attack_snapshot(hash_file: Path, wordlist: Path, rules: Path | None):
    """Fingerprint content and retain byte-exact immutable restore inputs.

    Inputs are read synchronously here — before any supervisor is spawned —
    but only through validated O_NONBLOCK|O_NOFOLLOW descriptors and only up
    to _MAX_SNAPSHOT_BYTES per file. The caller bounds this preparation
    inside the runtime budget by running it in a supervised subprocess.
    """
    import hashlib

    from .worker import private_read, validate_private

    contents = [
        _validated_input_bytes(hash_file),
        _validated_input_bytes(wordlist),
        _validated_input_bytes(rules) if rules else None,
    ]
    fingerprint = hashlib.sha256(
        json.dumps([hashlib.sha256(body).hexdigest() if body is not None else None for body in contents]).encode()
    ).hexdigest()
    # Scope by the same canonical export identity as the persistent lock.
    # Equal content in separately named exports must never share checkpoints.
    export_identity = hashlib.sha256(str(hash_file.resolve()).encode()).hexdigest()
    attack_dir = _ensure_dir(hash_file.parent / f"attack-{export_identity}-{fingerprint}")
    # Immutable inputs prevent a changed source file from corrupting an
    # in-flight job or a later restore of hashcat's saved argv.
    snapshots = []
    for name, body in zip(("hashes", "words", "rules"), contents, strict=True):
        snapshot = attack_dir / name
        if body is not None:
            try:
                saved = private_read(snapshot)
            except FileNotFoundError:
                _safe_write(snapshot, body)
            else:
                if saved != body:
                    msg = "attack snapshot content mismatch"
                    raise ValueError(msg)
        snapshots.append(snapshot)
    session = "oa-" + hashlib.sha256(str(attack_dir).encode()).hexdigest()[:32]
    restore = attack_dir / "checkpoint.restore"
    resumed = validate_private(restore, missing_ok=True)
    validate_private(Path(str(restore) + ".new"), missing_ok=True)
    return fingerprint, snapshots, session, restore, resumed


def _supervised_snapshot(hash_file: Path, wordlist: Path, rules: Path | None, deadline_monotonic: float) -> dict:
    """Run _attack_snapshot under a hard kill/reap deadline.

    Snapshot preparation reads input bytes; even validated reads can be
    slow on huge files. The work runs in a forked child that answers on a
    pipe; on deadline expiry the child is SIGKILLed and reaped — the audit
    lock fd it inherited closes with it, so no orphan holds the audit.
    """
    import contextlib
    import json
    import os
    import select
    import signal
    import time

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child path, exercised via launch tests
        code = 0
        try:
            os.close(read_fd)
            try:
                fingerprint, snapshots, session, restore, resumed = _attack_snapshot(hash_file, wordlist, rules)
                payload = json.dumps({
                    "fingerprint": fingerprint,
                    "snapshots": [str(s) for s in snapshots],
                    "session": session,
                    "restore": str(restore),
                    "resumed": resumed,
                }).encode()
            except BaseException as exc:
                payload = json.dumps({"error": str(exc)}).encode()
            with os.fdopen(write_fd, "wb") as stream:
                stream.write(payload)
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    os.close(write_fd)
    try:
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            msg = "audit preparation exceeded supervisor safety timeout"
            raise TimeoutError(msg)
        readable, _, _ = select.select([read_fd], [], [], remaining)
        if not readable:
            msg = "audit preparation exceeded supervisor safety timeout"
            raise TimeoutError(msg)
        chunks = []
        while True:
            chunk = os.read(read_fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
        if "error" in payload:
            raise ValueError(payload["error"])
        return payload
    finally:
        os.close(read_fd)
        # Hard deadline: kill and reap the child unconditionally. An
        # already-exited child is an unreaped zombie until this waitpid.
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def launch_hashcat(  # noqa: C901 - explicit launch/error/lock lifecycle
    hash_file: Path, wordlist: Path | None = None, rules: Path | None = None, runtime_budget: int = RUNTIME_BUDGET_S
) -> tuple[Path | None, str]:
    """All attacks detach; lock ownership and completion survive MCP restarts.

    The internal supervisor waits/reaps hashcat and atomically persists its
    exit code before releasing the lock. No PID-based liveness inference.
    Reruns auto-resume only a matching content fingerprint and checkpoint.
    Modern hashcat uses --restore-position; verified legacy checkpoints
    retain position but replace saved argv with the current gated command.
    The supervisor independently caps preparation and execution at budget.
    Remove the whole audit directory (including snapshots/checkpoints) only
    after completion. Never unlink a live audit's persistent lock inode.
    """
    import os
    import select
    import shutil
    import sys

    from .worker import atomic_json, validate_private

    started_monotonic = time.monotonic()
    try:
        budget = _runtime(runtime_budget)
    except (ValueError, TypeError, OverflowError) as exc:
        return None, str(exc)
    binary = shutil.which("hashcat")
    if not binary:
        return None, "hashcat not found on PATH — install it"
    if not wordlist:
        return None, "a wordlist is required for an attack"
    hash_file, wordlist = hash_file.resolve(), wordlist.resolve()
    try:
        fd = _audit_lock(hash_file)
    except BlockingIOError:
        return None, "a pass for this hash file is already running — poll password_crack_status"
    except OSError as exc:
        return None, str(exc)
    starter = None
    handshake_fds = []
    try:
        # Snapshot preparation (input byte reads, fingerprinting, snapshot
        # writes) is bounded by the same hard deadline as the whole launch:
        # a forked worker is SIGKILLed and reaped on expiry instead of
        # trusting cooperative deadline checks on the request path.
        prepared = _supervised_snapshot(hash_file, wordlist, rules, started_monotonic + budget)
        fingerprint = prepared["fingerprint"]
        snapshots = [Path(s) for s in prepared["snapshots"]]
        session, restore, resumed = prepared["session"], Path(prepared["restore"]), prepared["resumed"]
        validate_private(_potfile_path(hash_file), missing_ok=True)
        validate_private(hash_file.with_suffix(".log"), missing_ok=True)
        out = hash_file.with_suffix(".out")
        _clear_out_file(out)
        command = [
            binary,
            "-m",
            str(HASHCAT_MODE),
            "--username",
            "-o",
            str(out),
            "-a",
            "0",
            str(snapshots[0]),
            str(snapshots[1]),
            "--runtime",
            # Hashcat's integer runtime must leave shutdown inside our cap.
            str(max(1, int(budget - min(5.0, budget * 0.2)))),
            "--hwmon-disable",
            "--potfile-path",
            str(_potfile_path(hash_file)),
            "--session",
            session,
            "--restore-file-path",
            str(restore),
        ]
        if rules:
            command += ["-r", str(snapshots[2])]
        if resumed:
            # New hashcat --restore only PRINTS saved argv. Capability probe
            # runs off the request path in the supervisor, never on a GPU.
            resume_command = [binary, "--session", session, "--restore-file-path", str(restore), "--restore"]
        else:
            resume_command = None
        status = _status_path(hash_file)
        atomic_json(
            status,
            {
                "started": time.time(),
                "budget_s": budget,
                "deadline_monotonic": started_monotonic + budget,
                "command": command,
                "log": str(hash_file.with_suffix(".log")),
                "fingerprint": fingerprint,
                "session": session,
                "restore_path": str(restore),
                "resumed": resumed,
                "legacy_resume_command": resume_command,
                "returncode": None,
            },
        )
        if time.monotonic() >= started_monotonic + budget:
            msg = "audit preparation exceeded supervisor safety timeout"
            raise TimeoutError(msg)
        ready_read, ready_write = os.pipe()
        go_read, go_write = os.pipe()
        handshake_fds.extend((ready_read, ready_write, go_read, go_write))
        starter = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("worker.py")),
                str(status),
                str(fd),
                str(ready_write),
                str(go_read),
            ],
            pass_fds=(fd, ready_write, go_read),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(ready_write)
        handshake_fds.remove(ready_write)
        os.close(go_read)
        handshake_fds.remove(go_read)
        startup_deadline = min(started_monotonic + budget, time.monotonic() + 5)
        if starter.wait(timeout=min(5, max(0, startup_deadline - time.monotonic()))) != 0:
            msg = "supervisor failed to start"
            raise OSError(msg)  # noqa: TRY301 - common launcher cleanup
        readable, _, _ = select.select([ready_read], [], [], max(0, startup_deadline - time.monotonic()))
        if not readable or os.read(ready_read, 1) != b"R":
            msg = "supervisor startup handshake timed out"
            raise TimeoutError(msg)
        # Supervisor already owns the flock but cannot attack before GO.
        # Closing the pipe on failure cancels the handoff without PID signals.
        os.write(go_write, b"G")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if starter is not None and starter.poll() is None:
            starter.kill()
            starter.wait()
        if starter is not None:
            from .worker import private_read

            record = json.loads(private_read(_status_path(hash_file)))
            record.update(returncode=-1, error=str(exc))
            atomic_json(_status_path(hash_file), record)
        return None, str(exc)
    else:
        return status, ""
    finally:
        for handshake_fd in handshake_fds:
            os.close(handshake_fd)
        os.close(fd)


def crack_status(hash_file: Path) -> tuple[str | None, str]:
    """Retained durable completion, normalized output, cumulative audit potfile."""
    import os

    from .worker import private_read

    hash_file = hash_file.resolve()
    status = _status_path(hash_file)
    try:
        status.lstat()
    except FileNotFoundError:
        return None, "no launched pass for this hash file (launch with password_crack)"
    try:
        fd = _audit_lock(hash_file)
    except BlockingIOError:
        return "running: audit lock held; poll password_crack_status again", ""
    except OSError as exc:
        return None, f"invalid completion record: {exc}"
    try:
        st = json.loads(private_read(status))
        rc = st.get("returncode")
        # 3 = checkpoint quit (partial pass), 4 = runtime limit: both
        # successful bounded outcomes, not complete keyspace exhaustion.
        if rc not in (0, 1, 3, 4):
            reason = rc if rc is not None else "unknown; no exit record"
            return None, f"pass failed (hashcat exit {reason}): {st.get('error', '')}"
        hits = _relabel(_private_artifact_text(hash_file.with_suffix(".out")), hash_file)
        cumulative = _relabel(_private_artifact_text(_potfile_path(hash_file)), hash_file)
        hits = "\n".join(dict.fromkeys([*hits.splitlines(), *cumulative.splitlines()]))
    except (OSError, ValueError) as exc:
        return None, f"invalid completion record: {exc}"
    else:
        return hits or "(pass finished, no cracks)", ""
    finally:
        os.close(fd)


def _private_artifact_text(path: Path) -> str:
    """Read optional audit output through the validated no-follow descriptor."""
    from .worker import private_read

    try:
        return private_read(path).decode("utf-8")
    except FileNotFoundError:
        return ""


def run_hashcat(
    hash_file: Path,
    wordlist: Path | None = None,
    rules: Path | None = None,
    extra: list[str] | None = None,
    host: Host = LOCAL,
    runtime_budget: int = RUNTIME_BUDGET_S,
) -> tuple[str | None, str]:
    """Attacks always launch asynchronously; --show is bounded to 30 seconds.

    Poll crack_status for durable completion. --show reads stdout, never
    stale per-pass output. Caller-supplied hashcat flags are disallowed so
    global potfile/restore paths cannot silently override audit isolation.
    """
    import os
    import shutil

    hash_file = hash_file.resolve()

    if extra:
        return None, "extra hashcat flags are not supported"
    try:
        _runtime(runtime_budget)
    except (ValueError, TypeError, OverflowError) as exc:
        return None, str(exc)
    if wordlist:
        status, error = launch_hashcat(hash_file, wordlist, rules, runtime_budget)
        if error:
            return None, error
        return f"LAUNCHED: poll password_crack_status(hash_file). Status file: {status}", ""
    binary = shutil.which("hashcat")
    if not binary:
        return None, "hashcat not found on PATH — install it"
    try:
        fd = _audit_lock(hash_file)
    except BlockingIOError:
        return None, "a pass for this hash file is already running — poll password_crack_status"
    try:
        from .worker import validate_private

        validate_private(_potfile_path(hash_file), missing_ok=True)
        cmd = [
            binary,
            "-m",
            str(HASHCAT_MODE),
            "--username",
            "--hwmon-disable",
            "--potfile-path",
            str(_potfile_path(hash_file)),
            "--restore-disable",
            "--show",
            str(hash_file),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode not in (0, 1):
            return None, f"hashcat failed ({proc.returncode}): {proc.stderr.strip()[:500]}"
        return _relabel(proc.stdout, hash_file), ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    finally:
        os.close(fd)


def password_findings(hash_file: Path) -> tuple[list[dict] | None, str]:  # noqa: C901 - private read and lossless decoding
    """Consolidated cracks as structured data: `([{uid, login, digest,
    password, password_encoding, source}], error)`, built from the audit-local potfile
    (digest->plain) joined against the hash file (uid=login). The
    machine-readable answer the report aggregator needs — kills the
    "(not recorded)" class and the audit-local-vs-global potfile
    ambiguity, server-side. $HEX plains decode as UTF-8 where possible;
    otherwise password is a lossless hex string with password_encoding=hex.
    """
    from .worker import private_read

    pot_file = _potfile_path(hash_file)
    try:
        pot_text = private_read(pot_file).decode("utf-8")
    except FileNotFoundError:
        completed, error = crack_status(hash_file)
        if not error and completed == "(pass finished, no cracks)":
            return [], ""
        return None, f"no audit potfile {pot_file} — run password_crack with a wordlist first"
    except (OSError, ValueError) as exc:
        return None, f"invalid audit potfile: {exc}"
    pot: dict[str, str] = {}
    for line in pot_text.splitlines():
        fields = line.split(":")
        if len(fields) >= 5:
            pot[":".join(fields[:4])] = ":".join(fields[4:])  # plains may contain colons

    findings: list[dict] = []
    for line in hash_file.read_text().splitlines():
        if ":" not in line:
            continue
        label, _, rest = line.partition(":")
        parts = rest.split(":")
        if len(parts) < 4:
            continue
        uid, _, login = label.partition("=")
        login = _decode_label(f"{uid}={login}").partition("=")[2]
        digest = parts[3]
        plain = pot.get(":".join(parts[:4]))
        if plain is not None:
            encoding = "utf-8"
            if re.fullmatch(r"\$HEX\[(?:[0-9a-fA-F]{2})*\]", plain):
                raw = bytes.fromhex(plain[5:-1])
                try:
                    plain = raw.decode("utf-8")
                except UnicodeDecodeError:
                    plain, encoding = raw.hex(), "hex"
            findings.append({
                "uid": int(uid) if uid.isdigit() else uid,
                "login": login,
                "digest": digest,
                "password": plain,
                "password_encoding": encoding,
                "source": "audit-potfile",
            })
    return findings, ""


# ---------------------------------------------------------------------------
# calibration: how fast THIS machine cracks, to size the plan against the
# operator's time budget
# ---------------------------------------------------------------------------


def calibrate(  # noqa: C901 - deadline/error gates
    probe_rounds: int, timeout_s: float = 120, *, n_salts: int = 8
) -> tuple[float | None, str]:
    """Measure this machine's pbkdf2-sha512 cracking speed at exactly
    `probe_rounds` — the round count the target hashes actually use — with
    a tiny synthetic wordlist benchmark (n_salts unique salts, 200 candidates).
    Returns `(candidate_salt_pairs_per_second, error)`.
    n_salts must be an integer from 1 to 8; planners should supply the
    minimum actual work-group count (capped at 8) to avoid optimistic rates.

    No round-scaling arithmetic: on this hardware the throughput is
    latency-bound, not round-proportional (measured ~240 pairs/s at 25k
    rounds vs ~190 pairs/s at 600k — a naive linear model is off ~15x), so
    the benchmark must run at each actual production round bucket. Use
    read_hash_work_groups for unique salt/round groups, not exported rows.
    """
    import base64
    import math
    import shutil
    import subprocess
    import tempfile
    import time as _time

    started = _time.monotonic()
    try:
        timeout = float(timeout_s)
        if isinstance(timeout_s, bool) or not math.isfinite(timeout) or timeout <= 0:
            return None, "timeout_s must be finite and positive"
    except (ValueError, TypeError, OverflowError):
        return None, "timeout_s must be finite and positive"
    if isinstance(probe_rounds, bool) or not isinstance(probe_rounds, int) or probe_rounds <= 0:
        return None, "probe_rounds must be a positive integer"
    if isinstance(n_salts, bool) or not isinstance(n_salts, int) or not 1 <= n_salts <= 8:
        return None, "n_salts must be an integer from 1 to 8"
    deadline = started + timeout
    if not shutil.which("hashcat"):
        return None, "hashcat not found on PATH — install it (nvidia: `apt install hashcat`, needs opencl runtime)"

    # several synthetic hashes: a single hash leaves the GPU starved (real
    # runs batch many hashes), so the benchmark must match that shape —
    # measured: 1 hash ≈ 6x slower per candidate than 8 hashes
    ab = lambda b: base64.b64encode(b).decode().rstrip("=").replace("+", ".")
    lines = []
    for i in range(n_salts):
        salt = f"calib-{i:03d}-x".encode()
        # Arbitrary synthetic digest, not any candidate's password hash.
        # CPU PBKDF2 here would make preparation unbounded at high rounds.
        digest = bytes([i + 1]) * 64
        stored = f"$pbkdf2-sha512${probe_rounds}${ab(salt)}${ab(digest)}"
        lines.append(to_hashcat_line(f"{i}=bench", stored))
    with tempfile.TemporaryDirectory() as td:
        bench = Path(td) / "bench.hashcat"
        body = "\n".join([str(x) for x in lines]) + "\n"
        bench.write_text(body)
        bench.chmod(0o600)
        out = Path(td) / "bench.out"
        # wordlist attack (-a 0) over a slice of the tier-2 candidates: the
        # throughput to predict is the wordlist pass's — mask attacks
        # measure a different number on this hardware (~10-240 H/s mask vs
        # ~190 H/s wordlist at the same rounds; do not mix the two)
        wordlist = Path(td) / "bench.wordlist"
        probe_candidates = _TIER2_CANDIDATES[:200]
        probe_body = "\n".join([str(c) for c in probe_candidates]) + "\n"
        wordlist.write_text(probe_body)
        cmd = [
            "hashcat",
            "-m",
            str(HASHCAT_MODE),
            "--username",
            "-o",
            str(out),
            "-a",
            "0",
            str(bench),
            str(wordlist),
            "--potfile-disable",  # the probe candidates' plains must not be skipped
            "--restore-disable",  # calibration must not write global checkpoints
            "--hwmon-disable",  # laptop GPU: no NVML fan control, hwmon aborts (rc 4)
        ]
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return None, "calibration preparation timed out"
        t0 = _time.monotonic()
        remaining = min(remaining, deadline - t0)
        if remaining <= 0:
            return None, "calibration preparation timed out"
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired:
            return None, "calibration hashcat run timed out"
        except OSError as exc:
            return None, f"calibration hashcat failed: {exc}"
        ended = _time.monotonic()
        wall = ended - t0
        if ended > deadline:
            return None, "calibration hashcat run timed out"
        # 4 = "runtime limit reached" — the --runtime abort IS the success
        # path when the mask is bigger than the probe budget; 1 = exhausted
        if proc.returncode not in (0, 1, 4):
            tail = proc.stdout[-300:] + " | " + proc.stderr.strip()[-300:]
            return None, f"calibration hashcat failed ({proc.returncode}): {tail}"
        if wall <= 0:
            return None, "calibration produced no elapsed time"
    # status line "Progress" reports what was actually tried (a --runtime
    # cut may stop the mask early) — parse it instead of assuming the
    # whole mask ran
    m = re.search(r"Progress\.*:\s*(\d+)/(\d+)", proc.stdout)
    tried = int(m.group(1)) if m else 0
    if not tried:
        return None, "calibration produced no progress data"
    return tried / wall, ""  # candidate-hash pairs per second


def effective_speed(pairs_per_second: float, n_salts: int) -> float:
    """Compatibility shim: salt-based arbitrary throughput derating is removed."""
    return pairs_per_second


def estimate_candidate_seconds(
    candidate_count: int, n_hashes: int, pairs_per_second: float, n_salts: int | None = None
) -> float:
    """Legacy pairs/s estimate; n_salts is deprecated and ignored.

    New planners must count unique salt/round work groups, not user rows.
    """
    return candidate_count * n_hashes / pairs_per_second


# ---------------------------------------------------------------------------
# the Plugin class
# ---------------------------------------------------------------------------


class PasswordAuditPlugin(Plugin):
    """Contributes the MCP tools; no TUI tab (this is agent/operator work)."""

    name = "password-audit"
    default = False  # opt-in, and for good reason
    requires = ()  # talks to postgres directly, not through odooly
