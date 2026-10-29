"""ProviderConfigDbAdapter against real PostgreSQL.

Seeds the registry with scripts/seed_registry_dev.sql (so the dev seed is
tested too) plus one server with encrypted BasicAuth credentials, inserted
here because ciphertext cannot be written in plain SQL.

Runs only when UMP_TEST_DATABASE_URL points at a disposable database (see
test_migration_0003_registry.py). The registry tables are emptied per test.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ump.adapters.credential_cipher_fernet import FernetCredentialCipher
from ump.adapters.provider_config_db_adapter import ProviderConfigDbAdapter
from ump.core.models.providers_config import ProviderConfig

DB_URL = os.environ.get("UMP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="UMP_TEST_DATABASE_URL not set (needs PostgreSQL)"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_SQL = (REPO_ROOT / "scripts" / "seed_registry_dev.sql").read_text()
CIPHER = FernetCredentialCipher([Fernet.generate_key().decode()])
SHA = "a" * 64

# What the adapter must produce from the seeded rows.
EXPECTED = {
    "mock": ProviderConfig.model_validate(
        {
            "name": "mock",
            "url": "http://localhost:5001/",
            "processes": [
                {"id": pid, "description": title, "anonymous-access": True}
                for pid, title in (
                    ("echo", "Echo"),
                    ("failing-job", "Failing job"),
                    ("hello-world", "Hello World"),
                    ("slow", "Slow"),
                )
            ],
        }
    ),
    "srv": ProviderConfig.model_validate(
        {
            "name": "srv",
            "url": "http://srv.local/",
            "ttw-job-done": 60,
            "authentication": {
                "type": "BasicAuth",
                "user": "alice",
                "password": "s3cret",
            },
            "processes": [
                {
                    "id": "slow",
                    "description": "Slow model",
                    "version": "1.2",
                    "poll-interval": 2.5,
                    "ttw-job-done": 30,
                    "result-storage": "geoserver",
                    "result-path": "result.features",
                    "graph-properties": {
                        "root-path": "result",
                        "x-path": "x",
                        "y-path": "y",
                    },
                }
            ],
        }
    ),
}


@pytest.fixture(scope="module", autouse=True)
def migrated():
    if not DB_URL:
        return
    from alembic import command
    from alembic.config import Config

    os.environ["UMP_DATABASE_URL"] = DB_URL
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def engine(migrated):
    engine = create_async_engine(DB_URL)

    async def truncate():
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE registry.access_request, "
                    "registry.model, registry.provider_server, "
                    "registry.app_user"
                )
            )

    # before and after: other test modules share this database
    await truncate()
    await _seed(engine)
    yield engine
    await truncate()
    await engine.dispose()


async def _run_seed_script(engine) -> None:
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        # the seed file has several statements and its own BEGIN/COMMIT
        await raw.driver_connection.execute(SEED_SQL)


async def _seed(engine) -> None:
    await _run_seed_script(engine)

    secret = CIPHER.encrypt(json.dumps({"user": "alice", "password": "s3cret"}))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO registry.provider_server "
                "(name, base_url, owner_sub, auth_type, credentials,"
                " ttw_job_done_seconds) "
                "VALUES ('srv', 'http://srv.local/', 'dev-owner', 'BasicAuth', :c, 60)"
            ),
            {"c": secret},
        )
        await conn.execute(
            text("""
                INSERT INTO registry.model (
                    slug, provider_server_id, remote_process_id, owner_sub,
                    full_name, purpose, license, version_label, process_config,
                    status, published_at, verified_by, verified_interface_sha256)
                SELECT v.slug, ps.id, v.pid, 'dev-owner', v.name::jsonb,
                       '{"en": "x"}', 'MIT', v.version, v.config::jsonb,
                       v.status, now(), 'dev-verifier', :sha
                  FROM registry.provider_server ps,
                       (VALUES
                         ('srv-slow', 'slow', '{"en": "Slow model"}', '1.2',
                          '{"poll-interval": 2.5, "ttw-job-done": 30,
                            "result-storage": "geoserver",
                            "result-path": "result.features",
                            "graph-properties": {"root-path": "result",
                                                 "x-path": "x", "y-path": "y"}}',
                          'published'),
                         ('srv-wip', 'wip', '{"en": "Work in progress"}', NULL,
                          '{}', 'draft')
                       ) AS v(slug, pid, name, version, config, status)
                 WHERE ps.name = 'srv'
                """),
            {"sha": SHA},
        )


async def _until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _sorted(provider: ProviderConfig) -> ProviderConfig:
    return provider.model_copy(
        update={"processes": sorted(provider.processes, key=lambda p: p.id)}
    )


@pytest.mark.asyncio
async def test_snapshot_equals_expected_provider_configs(engine):
    async with ProviderConfigDbAdapter(engine, CIPHER, poll_interval=3600) as db:
        assert db.list_providers() == ["mock", "srv"]
        for name, expected in EXPECTED.items():
            # incl. decrypted credentials; drafts are not part of the snapshot
            assert _sorted(db.get_provider(name)) == expected
        assert db.check_process_availability("mock", "mock:echo")
        assert db.get_process_config("srv", "srv:wip") is None


@pytest.mark.asyncio
async def test_seed_script_is_idempotent(engine):
    await _run_seed_script(engine)
    async with engine.connect() as conn:
        count = await conn.scalar(text("SELECT count(*) FROM registry.model"))
    assert count == 6  # 4 mock + 2 srv, nothing duplicated


@pytest.mark.asyncio
async def test_status_controls_snapshot_and_notify_reaches_other_workers(engine):
    # Two adapters = two workers. No polling: only NOTIFY can update them.
    async with (
        ProviderConfigDbAdapter(engine, CIPHER, poll_interval=3600) as a,
        ProviderConfigDbAdapter(engine, CIPHER, poll_interval=3600) as b,
    ):
        await asyncio.sleep(0.3)  # let both LISTEN connections attach
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE registry.model SET status = 'deactivated' "
                    "WHERE slug = 'mock-echo'"
                )
            )
        for adapter in (a, b):
            assert await _until(
                lambda: adapter.get_process_config("mock", "mock:echo").state
                == "deactivated"
            )
            # still resolvable for running jobs, but not available
            assert adapter.get_provider("mock") is not None
            assert not adapter.check_process_availability("mock", "mock:echo")

        # back to draft: leaves the snapshot entirely
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE registry.model SET status = 'draft' "
                    "WHERE slug = 'mock-echo'"
                )
            )
        assert await _until(lambda: a.get_process_config("mock", "mock:echo") is None)


@pytest.mark.asyncio
async def test_undecryptable_provider_is_skipped(engine):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE registry.provider_server SET credentials = 'garbage' "
                "WHERE name = 'srv'"
            )
        )
    async with ProviderConfigDbAdapter(engine, CIPHER, poll_interval=3600) as db:
        assert db.list_providers() == ["mock"]


@pytest.mark.asyncio
async def test_failed_refresh_keeps_previous_snapshot(engine):
    async with ProviderConfigDbAdapter(engine, CIPHER, poll_interval=3600) as db:
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER VIEW registry.v_providers_config RENAME TO v_moved")
            )
        try:
            assert await db.refresh() is False
            assert db.list_providers() == ["mock", "srv"]
        finally:
            async with engine.begin() as conn:
                await conn.execute(
                    text("ALTER VIEW registry.v_moved RENAME TO v_providers_config")
                )
