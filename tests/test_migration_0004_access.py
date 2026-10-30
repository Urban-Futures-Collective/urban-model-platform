"""Migration 0004 (access_request) against a real PostgreSQL.

Runs only with UMP_TEST_DATABASE_URL (see test_migration_0003_registry.py);
upgrades and downgrades that database, so never point it at real data.
"""

import asyncio
import os
from pathlib import Path

import pytest

DB_URL = os.environ.get("UMP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="UMP_TEST_DATABASE_URL not set (needs PostgreSQL)"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "00000000-0000-0000-0000-0000000000a1"


def _alembic(monkeypatch, *args: str) -> None:
    from alembic import command
    from alembic.config import Config

    monkeypatch.setenv("UMP_DATABASE_URL", DB_URL)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    getattr(command, args[0])(cfg, *args[1:])


def _run(*statements: str):
    """Execute statements in one rolled-back transaction; return last fetch."""
    import asyncpg

    async def go():
        conn = await asyncpg.connect(DB_URL.replace("postgresql+asyncpg", "postgresql"))
        try:
            tx = conn.transaction()
            await tx.start()
            try:
                result = None
                for stmt in statements:
                    result = await conn.fetch(stmt)
                return result
            finally:
                await tx.rollback()
        finally:
            await conn.close()

    return asyncio.run(go())


SEED = (
    "INSERT INTO registry.app_user (sub) VALUES ('owner'), ('user'), ('admin')",
    "INSERT INTO registry.provider_server (id, name, base_url) "
    "VALUES ('00000000-0000-0000-0000-000000000001', 'srv', 'http://srv')",
    "INSERT INTO registry.model "
    "(id, slug, provider_server_id, remote_process_id, owner_sub, full_name) "
    f"VALUES ('{MODEL_ID}', 'm1', '00000000-0000-0000-0000-000000000001', 'p1', "
    """'owner', '{"en": "Model"}')""",
)


def _request(**overrides: str) -> str:
    cols = {"user_sub": "'user'", "model_id": f"'{MODEL_ID}'"}
    cols.update(overrides)
    return (
        f"INSERT INTO registry.access_request ({', '.join(cols)}) "
        f"VALUES ({', '.join(cols.values())})"
    )


@pytest.fixture
def migrated(monkeypatch):
    _alembic(monkeypatch, "upgrade", "head")
    yield
    _alembic(monkeypatch, "upgrade", "head")


def test_defaults(migrated):
    rows = _run(
        *SEED,
        _request(),
        "SELECT status, id, status_changed_at, created_at, updated_at "
        "FROM registry.access_request",
    )
    row = rows[0]
    assert row["status"] == "requested"
    assert row["id"] and row["status_changed_at"] and row["created_at"]


def test_new_open_request_allowed_after_the_last_one_closed(migrated):
    rows = _run(
        *SEED,
        _request(status="'denied'", status_note="'no'", status_changed_by="'admin'"),
        _request(status="'withdrawn'"),
        _request(status="'revoked'", status_note="'gone'"),
        _request(),  # the one open record
        "SELECT count(*) AS n FROM registry.access_request",
    )
    assert rows[0]["n"] == 4


@pytest.mark.parametrize(
    "statements",
    [
        # one open record per user and model
        (_request(), _request()),
        (
            _request(status="'granted'", status_changed_by="'admin'"),
            _request(),
        ),
        # nobody decides on their own request
        (_request(status="'granted'", status_changed_by="'user'"),),
        (_request(status="'denied'", status_changed_by="'user'", status_note="'x'"),),
        # deny and revoke need a note
        (_request(status="'denied'", status_changed_by="'admin'"),),
        (_request(status="'revoked'", status_changed_by="'admin'"),),
        # unknown status, unknown user, unknown model
        (_request(status="'approved'"),),
        (_request(user_sub="'ghost'"),),
        (_request(model_id="'00000000-0000-0000-0000-00000000dead'"),),
    ],
)
def test_constraints(migrated, statements):
    import asyncpg

    with pytest.raises(asyncpg.PostgresError):
        _run(*SEED, *statements)


def test_downgrade_drops_access_request_only(migrated, monkeypatch):
    _alembic(monkeypatch, "downgrade", "0003")
    rows = _run(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'registry' ORDER BY table_name"
    )
    names = [r["table_name"] for r in rows]
    assert "access_request" not in names
    assert {"app_user", "model", "provider_server", "audit_log"} <= set(names)
