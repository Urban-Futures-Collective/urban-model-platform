"""ProviderConfigDbAdapter + SQLModelRegistryRepository against real PostgreSQL.

Runs only when UMP_TEST_DATABASE_URL points at a disposable database (see
test_migration_0003_registry.py). The registry tables are emptied per test.
"""

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ump.adapters.credential_cipher_fernet import FernetCredentialCipher
from ump.adapters.provider_config_db_adapter import ProviderConfigDbAdapter
from ump.adapters.provider_config_file_adapter import ProviderConfigFileAdapter
from ump.adapters.registry_repository_sql import SQLModelRegistryRepository
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
from ump.core.services.providers_import import ProvidersImportService

DB_URL = os.environ.get("UMP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="UMP_TEST_DATABASE_URL not set (needs PostgreSQL)"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CIPHER = FernetCredentialCipher([Fernet.generate_key().decode()])

PROVIDERS_YAML = """
providers:
  - name: srv
    url: http://srv.local
    ttw-job-done: 60
    authentication:
      type: BasicAuth
      user: alice
      password: s3cret
    processes:
      - id: echo
        description: Echo model
        anonymous-access: true
        deterministic: true
      - id: slow
        description: Slow model
        version: "1.2"
        poll-interval: 2.5
        ttw-job-done: 30
        result-storage: geoserver
        result-path: result.features
        graph-properties:
          root-path: result
          x-path: x
          y-path: y
      - id: "off"
        description: Excluded
        exclude: true
  - name: pub
    url: http://pub.local/api/
    processes:
      - id: hello
        description: Hello
"""


class StaticHttp:
    """HttpClientPort stand-in answering every description request."""

    async def get(self, url, timeout=None, headers=None):
        pid = url.rsplit("/", 1)[-1]
        return {"id": pid, "title": pid, "inputs": {}, "outputs": {}}


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
                    "TRUNCATE registry.model, registry.provider_server, "
                    "registry.app_user"
                )
            )

    # before and after: other test modules share this database
    await truncate()
    yield engine
    await truncate()
    await engine.dispose()


@pytest.fixture
def file_adapter(tmp_path):
    path = tmp_path / "providers.yaml"
    path.write_text(PROVIDERS_YAML)
    adapter = ProviderConfigFileAdapter(str(path))
    assert adapter.list_providers() == ["srv", "pub"]
    return adapter


async def _import(engine, file_adapter):
    registry = SQLModelRegistryRepository(
        async_sessionmaker(engine, expire_on_commit=False)
    )
    service = ProvidersImportService(
        registry, StaticHttp(), RemoteAuthAdapter(), CIPHER
    )
    report = await service.import_providers(file_adapter.get_providers(), "admin")
    assert report.ok, report.failures
    return report


async def _until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _without_excluded(provider):
    processes = sorted(
        (p for p in provider.processes if not p.exclude), key=lambda p: p.id
    )
    return provider.model_copy(update={"processes": processes})


@pytest.mark.asyncio
async def test_round_trip_equals_file_adapter(engine, file_adapter):
    report = await _import(engine, file_adapter)
    assert report.models_created == ["srv:echo", "srv:slow", "pub:hello"]

    async with ProviderConfigDbAdapter(engine, CIPHER, poll_interval=3600) as db:
        assert sorted(db.list_providers()) == ["pub", "srv"]
        for name in ("srv", "pub"):
            expected = _without_excluded(file_adapter.get_provider(name))
            actual = db.get_provider(name)
            actual = actual.model_copy(
                update={"processes": sorted(actual.processes, key=lambda p: p.id)}
            )
            assert actual == expected  # incl. decrypted credentials
        assert db.check_process_availability("srv", "srv:echo")
        assert db.get_process_config("srv", "srv:off") is None


@pytest.mark.asyncio
async def test_reimport_is_idempotent(engine, file_adapter):
    await _import(engine, file_adapter)
    report = await _import(engine, file_adapter)
    assert report.models_created == [] and report.servers_created == []
    assert report.models_existing == ["srv:echo", "srv:slow", "pub:hello"]


@pytest.mark.asyncio
async def test_status_controls_snapshot_and_notify_reaches_other_workers(
    engine, file_adapter
):
    await _import(engine, file_adapter)
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
                    "WHERE remote_process_id = 'echo'"
                )
            )
        for adapter in (a, b):
            assert await _until(
                lambda: adapter.get_process_config("srv", "srv:echo").state
                == "deactivated"
            )
            # still resolvable for running jobs, but not available
            assert adapter.get_provider("srv") is not None
            assert not adapter.check_process_availability("srv", "srv:echo")

        # back to draft: leaves the snapshot entirely
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE registry.model SET status = 'draft' "
                    "WHERE remote_process_id = 'echo'"
                )
            )
        assert await _until(lambda: a.get_process_config("srv", "srv:echo") is None)


@pytest.mark.asyncio
async def test_undecryptable_provider_is_skipped(engine, file_adapter):
    await _import(engine, file_adapter)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE registry.provider_server SET credentials = 'garbage' "
                "WHERE name = 'srv'"
            )
        )
    async with ProviderConfigDbAdapter(engine, CIPHER, poll_interval=3600) as db:
        assert db.list_providers() == ["pub"]


@pytest.mark.asyncio
async def test_failed_refresh_keeps_previous_snapshot(engine, file_adapter):
    await _import(engine, file_adapter)
    async with ProviderConfigDbAdapter(engine, CIPHER, poll_interval=3600) as db:
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER VIEW registry.v_providers_config RENAME TO v_moved")
            )
        try:
            assert await db.refresh() is False
            assert sorted(db.list_providers()) == ["pub", "srv"]
        finally:
            async with engine.begin() as conn:
                await conn.execute(
                    text("ALTER VIEW registry.v_moved RENAME TO v_providers_config")
                )
