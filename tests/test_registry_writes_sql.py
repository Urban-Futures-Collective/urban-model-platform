"""SQLModelRegistryRepository writes against real PostgreSQL.

The decision behind the port: a change and its audit row are committed
together or not at all. Checked for success, unique conflicts, stale
(optimistic-concurrency) updates and an all-or-nothing batch. Runs only with
UMP_TEST_DATABASE_URL (see test_migration_0003_registry.py).
"""

import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ump.adapters.registry_repository_sql import SQLModelRegistryRepository
from ump.core.interfaces.model_registry import RegistryConflictError, StaleWriteError
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.audit import AuditEvent

DB_URL = os.environ.get("UMP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="UMP_TEST_DATABASE_URL not set (needs PostgreSQL)"
)
REPO_ROOT = Path(__file__).resolve().parents[1]


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
async def db(migrated):
    engine = create_async_engine(DB_URL)

    async def truncate():
        async with engine.begin() as conn:
            # TRUNCATE does not fire the row-level audit guard
            await conn.execute(
                text(
                    "TRUNCATE registry.model, registry.provider_server, "
                    "registry.app_user, registry.audit_log"
                )
            )

    await truncate()
    repo = SQLModelRegistryRepository(
        async_sessionmaker(engine, expire_on_commit=False)
    )
    await repo.upsert_user(AppUser(sub="owner"))

    async def audit_actions():
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT action FROM registry.audit_log ORDER BY id")
            )
            return [r[0] for r in rows]

    yield repo, audit_actions
    await truncate()
    await engine.dispose()


def _event(action: str) -> AuditEvent:
    return AuditEvent(
        actor_sub="owner",
        actor_roles=["provider"],
        action=action,
        details={"k": "v"},
        request_id="rid-1",
    )


def _model(server: ProviderServer, pid: str, slug: str | None = None) -> RegistryModel:
    return RegistryModel(
        slug=slug or f"{server.name}-{pid}",
        provider_server_id=server.id,
        remote_process_id=pid,
        owner_sub="owner",
        full_name={"de": pid},
    )


@pytest.mark.asyncio
async def test_create_and_update_write_their_audit_rows(db):
    repo, audit_actions = db
    server = await repo.create_provider_server(
        ProviderServer(name="srv", base_url="https://srv/", owner_sub="owner"),
        audit=_event("server.create"),
    )
    updated = await repo.update_provider_server(
        server.model_copy(update={"ttw_job_done_seconds": 60.0, "name": "renamed"}),
        expected_updated_at=server.updated_at,
        audit=_event("server.update"),
    )
    assert updated.ttw_job_done_seconds == 60.0
    assert updated.name == "srv"  # the name is never rewritten
    assert updated.updated_at > server.updated_at  # new ETag
    assert await audit_actions() == ["server.create", "server.update"]


@pytest.mark.asyncio
async def test_conflict_writes_neither_row_nor_audit(db):
    repo, audit_actions = db
    server = ProviderServer(name="srv", base_url="https://srv/")
    await repo.create_provider_server(server, audit=_event("server.create"))
    with pytest.raises(RegistryConflictError):
        await repo.create_provider_server(
            ProviderServer(name="srv", base_url="https://other/"),
            audit=_event("server.create.again"),
        )
    assert await audit_actions() == ["server.create"]


@pytest.mark.asyncio
async def test_stale_update_writes_nothing(db):
    repo, audit_actions = db
    server = await repo.create_provider_server(
        ProviderServer(name="srv", base_url="https://srv/"),
        audit=_event("server.create"),
    )
    (model,) = await repo.create_models(
        [_model(server, "p1")], audit=[_event("model.create")]
    )
    first = await repo.update_model(
        model.model_copy(update={"code_ref": "v1"}),
        expected_updated_at=model.updated_at,
        audit=_event("model.card.update"),
    )
    with pytest.raises(StaleWriteError):
        await repo.update_model(  # based on the old version
            model.model_copy(update={"code_ref": "v2"}),
            expected_updated_at=model.updated_at,
            audit=_event("model.card.update.stale"),
        )
    assert (await repo.get_model_by_id(model.id)).code_ref == first.code_ref == "v1"
    assert await audit_actions() == [
        "server.create",
        "model.create",
        "model.card.update",
    ]


@pytest.mark.asyncio
async def test_batch_create_is_all_or_nothing(db):
    repo, audit_actions = db
    server = await repo.create_provider_server(
        ProviderServer(name="srv", base_url="https://srv/"),
        audit=_event("server.create"),
    )
    await repo.create_models([_model(server, "p1")], audit=[_event("model.create")])
    with pytest.raises(RegistryConflictError):
        await repo.create_models(
            [_model(server, "p2"), _model(server, "p3", slug="srv-p1")],  # slug taken
            audit=[_event("model.create.p2"), _event("model.create.p3")],
        )
    assert await repo.get_model(server.id, "p2") is None
    assert await repo.get_model_by_slug("srv-p1") is not None
    assert await audit_actions() == ["server.create", "model.create"]


@pytest.mark.asyncio
async def test_audit_row_content(db):
    repo, _ = db
    server = await repo.create_provider_server(
        ProviderServer(name="srv", base_url="https://srv/"),
        audit=_event("server.create"),
    )
    draft = _model(server, "p1")
    await repo.create_models(
        [draft],
        audit=[
            AuditEvent(
                actor_sub="owner",
                actor_roles=["provider", "viewer"],
                action="model.create",
                model_id=draft.id,
                details={"slug": "srv-p1"},
                request_id="rid-2",
            )
        ],
    )
    async with repo._session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT actor_sub, actor_roles, model_id, outcome, details, "
                    "request_id, occurred_at FROM registry.audit_log "
                    "WHERE action = 'model.create'"
                )
            )
        ).one()
    assert row.actor_sub == "owner"
    assert row.actor_roles == ["provider", "viewer"]
    assert row.model_id == draft.id
    assert row.outcome == "success" and row.details == {"slug": "srv-p1"}
    assert row.request_id == "rid-2" and row.occurred_at is not None
