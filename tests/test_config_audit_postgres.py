"""Exercise PostgreSQL relation resolution, which sqlite cannot emulate."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from odoo_activity.plugins.config_audit import _COMPANY_COLUMNS_SQL


def test_company_columns_follow_search_path(tmp_path):
    initdb = shutil.which("initdb")
    if not initdb:
        initdb = next(iter(sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"))), None)
    if not initdb or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("requires PostgreSQL server binaries and a non-root user")
    bindir = Path(initdb).parent
    data = tmp_path / "data"

    def run(binary, *args, input_text=None):
        return subprocess.run(  # noqa: S603 — local PostgreSQL binaries, fixed test arguments
            [str(bindir / binary), *map(str, args)],
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
        ).stdout

    run("initdb", "-D", data, "-A", "trust", "-U", "audit_test", "--no-locale")
    # Keep the Unix socket path short even under deeply nested pytest temp dirs.
    with tempfile.TemporaryDirectory(prefix="oa-pg-") as socket_dir:
        run("pg_ctl", "-D", data, "-l", tmp_path / "postgres.log", "-o", f"-k {socket_dir} -h ''", "-w", "start")
        try:
            output = run(
                "psql",
                "-X",
                "-h",
                socket_dir,
                "-U",
                "audit_test",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-Atq",
                input_text=f"""
                    CREATE SCHEMA preceding;
                    CREATE TABLE public.res_company (password_history integer, secret text);
                    SET search_path = preceding, public;
                    {_COMPANY_COLUMNS_SQL};
                    CREATE TABLE preceding.res_company (password_lower integer, password_upper integer);
                    ALTER TABLE preceding.res_company DROP COLUMN password_upper;
                    {_COMPANY_COLUMNS_SQL};
                """,
            )
            # First find public's table; then honor a table shadowing it.
            assert output.splitlines() == ["password_history", "password_lower"]
        finally:
            run("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")
