"""SQLModelRegistryRepository: access requests against real PostgreSQL.

Change and audit row in one transaction; the open-record rule as a conflict;
optimistic concurrency; the list query (visibility, filters, keyset order)
checked against ``AccessQuery.matches``. Runs only with UMP_TEST_DATABASE_URL
(see test_migration_0003_registry.py).
"""

import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ump.adapters.registry_repository_sql import SQLModelRegistryRepository
from ump.core.interfaces.model_registry import (
    AccessQuery,
    RegistryConflictError,
    StaleWriteError,
)
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.access import AccessRequest
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
                    "TRUNCATE registry.access_request, registry.model, "
                    "registry.provider_server, registry.app_user, registry.audit_log"
                )
            )

    await truncate()
    repo = SQLModelRegistryRepository(
        async_sessionmaker(engine, expire_on_commit=False)
    )
    for sub in ("owner", "owner2", "user", "user2", "admin"):
        await repo.upsert_user(AppUser(sub=sub))
    server = await repo.create_provider_server(
        ProviderServer(name="srv", base_url="https://srv/"), audit=_event("seed")
    )
    models = await repo.create_models(
        [
            RegistryModel(
                slug=f"srv-{pid}",
                provider_server_id=server.id,
                remote_process_id=pid,
                owner_sub=owner,
                full_name={"en": pid},
            )
            for pid, owner in (("a", "owner"), ("b", "owner2"))
        ],
        audit=[],
    )

    async def audit_actions():
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT action FROM registry.audit_log "
                    "WHERE action LIKE 'access.%' ORDER BY id"
                )
            )
            return [r[0] for r in rows]

    yield repo, models, audit_actions
    await truncate()
    await engine.dispose()


def _event(action: str) -> AuditEvent:
    return AuditEvent(actor_sub="admin", actor_roles=["access-admin"], action=action)


@pytest.mark.asyncio
async def test_create_update_and_audit(db):
    repo, (model, _), audit_actions = db
    created = await repo.create_access_request(
        AccessRequest(user_sub="user", model_id=model.id, request_note="please"),
        audit=_event("access.request"),
    )
    assert created.status == "requested"
    assert created.created_at and created.status_changed_at and created.updated_at

    granted = await repo.update_access_request(
        created.model_copy(
            update={
                "status": "granted",
                "status_changed_by": "admin",
                "request_note": "rewritten",  # not a step field: ignored
                "user_sub": "user2",  # identity: ignored
            }
        ),
        expected_updated_at=created.updated_at,
        audit=_event("access.grant"),
    )
    assert granted.status == "granted" and granted.status_changed_by == "admin"
    assert granted.request_note == "please" and granted.user_sub == "user"
    assert granted.updated_at > created.updated_at
    assert await repo.get_access_request(created.id) == granted
    assert await audit_actions() == ["access.request", "access.grant"]


@pytest.mark.asyncio
async def test_second_open_record_conflicts_without_audit(db):
    repo, (model, _), audit_actions = db
    await repo.create_access_request(
        AccessRequest(user_sub="user", model_id=model.id),
        audit=_event("access.request"),
    )
    with pytest.raises(RegistryConflictError):
        await repo.create_access_request(
            AccessRequest(user_sub="user", model_id=model.id),
            audit=_event("access.request.again"),
        )
    assert await audit_actions() == ["access.request"]


@pytest.mark.asyncio
async def test_stale_update_writes_nothing(db):
    repo, (model, _), audit_actions = db
    created = await repo.create_access_request(
        AccessRequest(user_sub="user", model_id=model.id),
        audit=_event("access.request"),
    )
    with pytest.raises(StaleWriteError):
        await repo.update_access_request(
            created.model_copy(update={"status": "withdrawn"}),
            expected_updated_at=created.created_at.replace(year=2000),
            audit=_event("access.withdraw"),
        )
    assert (await repo.get_access_request(created.id)).status == "requested"
    assert await audit_actions() == ["access.request"]


@pytest.mark.asyncio
async def test_ensure_user_never_overwrites(db):
    repo, _, _ = db
    await repo.upsert_user(AppUser(sub="known", email="token@example.org"))
    kept = await repo.ensure_user(AppUser(sub="known", email="admin@example.org"))
    assert kept.email == "token@example.org"
    new = await repo.ensure_user(AppUser(sub="new", email="admin@example.org"))
    assert new.email == "admin@example.org" and new.first_seen_at
    users = await repo.get_users({"known", "new", "ghost"})
    assert set(users) == {"known", "new"}


@pytest.mark.asyncio
async def test_list_matches_reference_semantics(db):
    repo, (model_a, model_b), _ = db
    stored = []
    for user, model, status in (
        ("user", model_a, "requested"),
        ("user", model_b, "withdrawn"),
        ("user2", model_a, "denied"),
        ("user2", model_b, "requested"),
        ("owner", model_b, "requested"),
    ):
        stored.append(
            await repo.create_access_request(
                AccessRequest(
                    user_sub=user,
                    model_id=model.id,
                    status=status,
                    status_note="n" if status == "denied" else None,
                    status_changed_by="admin" if status == "denied" else None,
                ),
                audit=_event("access.request"),
            )
        )
    owners = {model_a.id: "owner", model_b.id: "owner2"}
    newest_first = sorted(stored, key=lambda r: (r.created_at, r.id), reverse=True)
    queries = [
        AccessQuery(),
        AccessQuery(visible_to="user"),
        AccessQuery(visible_to="owner"),  # own requests + requests on own models
        AccessQuery(visible_to="owner2", status="requested"),
        AccessQuery(model_id=model_b.id),
        AccessQuery(user_sub="user2"),
        AccessQuery(limit=2),
        AccessQuery(before=(newest_first[1].created_at, newest_first[1].id)),
    ]
    for query in queries:
        expected = [r.id for r in newest_first if query.matches(r, owners[r.model_id])]
        got = [r.id for r in await repo.list_access_requests(query)]
        assert got == expected[: query.limit], query
