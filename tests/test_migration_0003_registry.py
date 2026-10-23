"""Migration 0003 (registry schema) against a real PostgreSQL.

Runs only when UMP_TEST_DATABASE_URL points at a disposable database, e.g.

    docker run -d --rm -e POSTGRES_USER=ump -e POSTGRES_PASSWORD=ump \
        -e POSTGRES_DB=ump -p 55432:5432 postgres:18.4
    UMP_TEST_DATABASE_URL=postgresql+asyncpg://ump:ump@localhost:55432/ump \
        poetry run pytest tests/test_migration_0003_registry.py

The tests upgrade/downgrade the schema of that database, so never point it at
real data.
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
SHA = "a" * 64


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
    "INSERT INTO registry.app_user (sub) VALUES ('owner'), ('verifier')",
    "INSERT INTO registry.provider_server (id, name, base_url) "
    "VALUES ('00000000-0000-0000-0000-000000000001', 'srv', 'http://srv')",
)


def _model(**overrides: str) -> str:
    cols = {
        "slug": "'m1'",
        "provider_server_id": "'00000000-0000-0000-0000-000000000001'",
        "remote_process_id": "'p1'",
        "owner_sub": "'owner'",
        "full_name": """'{"de": "Modell"}'""",
    }
    cols.update(overrides)
    return (
        f"INSERT INTO registry.model ({', '.join(cols)}) "
        f"VALUES ({', '.join(cols.values())})"
    )


@pytest.fixture
def migrated(monkeypatch):
    _alembic(monkeypatch, "upgrade", "head")
    yield
    _alembic(monkeypatch, "upgrade", "head")


def test_downgrade_removes_registry_schema(migrated, monkeypatch):
    _alembic(monkeypatch, "downgrade", "0002")
    rows = _run(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'registry'"
    )
    assert rows == []


def test_exec_state_is_generated_from_status(migrated):
    rows = _run(
        *SEED,
        _model(),
        "UPDATE registry.model SET status = 'rejected', purpose = '{\"de\": \"x\"}', "
        "license = 'MIT'",
        "SELECT exec_state FROM registry.model",
    )
    assert rows[0]["exec_state"] == "deactivated"


def test_published_model_appears_in_providers_view(migrated):
    rows = _run(
        *SEED,
        _model(
            status="'published'",
            purpose="""'{"de": "Zweck"}'""",
            license="'MIT'",
            verified_by="'verifier'",
            verified_interface_sha256=f"'{SHA}'",
        ),
        "SELECT name, processes FROM registry.v_providers_config",
    )
    assert rows[0]["name"] == "srv"
    assert '"state": "published"' in rows[0]["processes"]


@pytest.mark.parametrize(
    "statement",
    [
        # four-eyes: owner may not verify own model
        _model(verified_by="'owner'"),
        # full_name must contain default_lang
        _model(full_name="""'{"en": "Model"}'"""),
        # card must be complete before review
        _model(status="'in_review'"),
        # published models must carry a verified interface
        _model(status="'published'", purpose="""'{"de": "x"}'""", license="'MIT'"),
        # credentials present iff auth_type <> NoAuth
        "INSERT INTO registry.provider_server (name, base_url, auth_type) "
        "VALUES ('srv2', 'http://x', 'ApiKey')",
    ],
)
def test_invariants_are_enforced(migrated, statement):
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        _run(*SEED, statement)


def test_server_process_pair_is_unique(migrated):
    import asyncpg

    with pytest.raises(asyncpg.UniqueViolationError):
        _run(*SEED, _model(), _model(slug="'m2'"))


def test_audit_log_has_no_fk_and_is_append_only(migrated):
    import asyncpg

    rows = _run(
        "INSERT INTO registry.audit_log (action, model_id, job_id) "
        "VALUES ('job.execute', gen_random_uuid(), 'no-such-job') RETURNING id"
    )
    assert rows

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        _run(
            "INSERT INTO registry.audit_log (action) VALUES ('x')",
            "DELETE FROM registry.audit_log",
        )

    # the retention job may delete
    _run(
        "INSERT INTO registry.audit_log (action) VALUES ('x')",
        "SET LOCAL ump.audit_retention = 'on'",
        "DELETE FROM registry.audit_log",
    )
