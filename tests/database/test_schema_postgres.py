"""Apply database/schema.sql to a scratch PostgreSQL database and run the constraint checks.

Set VIA_TEST_PG_DSN to a server DSN whose user may create databases, e.g.
postgresql://postgres@localhost:5432/postgres
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
DSN = os.environ.get("VIA_TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN or not shutil.which("psql"), reason="VIA_TEST_PG_DSN not set or psql missing"),
]


def psql(dsn, *args):
    return subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", *args], capture_output=True, text=True)


def test_schema_and_constraints():
    name = f"via_test_{uuid.uuid4().hex[:8]}"
    created = psql(DSN, "-c", f"CREATE DATABASE {name}")
    assert created.returncode == 0, created.stderr
    parts = urlsplit(DSN)
    target = urlunsplit(parts._replace(path=f"/{name}"))
    try:
        schema = psql(target, "-f", str(ROOT / "database" / "schema.sql"))
        assert schema.returncode == 0, schema.stderr
        checks = psql(target, "-f", str(ROOT / "database" / "tests" / "constraint_checks.sql"))
        assert checks.returncode == 0, checks.stderr
        assert "constraint checks passed" in checks.stdout
    finally:
        psql(DSN, "-c", f"DROP DATABASE IF EXISTS {name}")
