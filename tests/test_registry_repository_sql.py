"""SQLModelRegistryRepository read side against real PostgreSQL.

Parity test: the same data in the SQL and the in-memory repository must give
the same answer to every ModelQuery (``ModelQuery.matches`` is the reference
semantics). Runs only with UMP_TEST_DATABASE_URL (see
test_migration_0003_registry.py); the registry tables are emptied per test.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository
from ump.adapters.registry_repository_sql import SQLModelRegistryRepository
from ump.core.interfaces.model_registry import ModelQuery
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.audit import AuditEvent

DB_URL = os.environ.get("UMP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="UMP_TEST_DATABASE_URL not set (needs PostgreSQL)"
)
REPO_ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
SHA = "b" * 64
SEED = AuditEvent(actor_sub=None, action="test.seed")


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
async def sql_repo(migrated):
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

    await truncate()
    yield SQLModelRegistryRepository(async_sessionmaker(engine, expire_on_commit=False))
    await truncate()
    await engine.dispose()


def _models(srv_a: ProviderServer, srv_b: ProviderServer) -> list[RegistryModel]:
    rows = [
        (srv_a, "p1", "published", "u1"),
        (srv_a, "p2", "draft", "u1"),
        (srv_a, "p3", "in_review", "u2"),
        (srv_b, "p4", "published", "u2"),
        (srv_b, "p5", "deactivated", "u1"),
        (srv_b, "p6", "submitted", "u2"),
    ]
    out = []
    for server, pid, status, owner in rows:
        verified = status in ("published",)
        out.append(
            RegistryModel(
                slug=f"{server.name}-{pid}",
                provider_server_id=server.id,
                remote_process_id=pid,
                owner_sub=owner,
                full_name={"de": pid},
                purpose={"de": "x"},
                license="MIT",
                status=status,
                published_at=T0 if status in ("published", "deactivated") else None,
                verified_by="v" if verified else None,
                verified_interface_sha256=SHA if verified else None,
                applications=[{"title": {"de": "A"}}],
                io_notes={"in": {"de": "m"}},
            )
        )
    return out


async def _seed(repo):
    for sub in ("u1", "u2", "v"):
        await repo.upsert_user(AppUser(sub=sub))
    a = await repo.create_provider_server(
        ProviderServer(name="a", base_url="http://a/", owner_sub="u1"), audit=SEED
    )
    b = await repo.create_provider_server(
        ProviderServer(name="b", base_url="http://b/", owner_sub="u2"), audit=SEED
    )
    models = _models(a, b)
    await repo.create_models(models, audit=[SEED] * len(models))
    return a, b


QUERIES = [
    lambda a, b: ModelQuery(),
    lambda a, b: ModelQuery(visible_statuses=frozenset({"published"})),
    lambda a, b: ModelQuery(
        visible_statuses=frozenset({"published"}), visible_owner="u1"
    ),
    lambda a, b: ModelQuery(visible_statuses=frozenset(), visible_owner="u2"),
    lambda a, b: ModelQuery(status="published"),
    lambda a, b: ModelQuery(
        visible_statuses=frozenset({"published"}), visible_owner="u1", status="draft"
    ),
    lambda a, b: ModelQuery(owner_sub="u1"),
    lambda a, b: ModelQuery(provider_server_id=b.id),
    lambda a, b: ModelQuery(after_slug="a-p3", limit=2),
]


@pytest.mark.asyncio
async def test_list_models_matches_in_memory_reference(sql_repo):
    memory = InMemoryRegistryRepository()
    a_sql, b_sql = await _seed(sql_repo)
    # same ids in both repositories, so provider filters line up
    for server in (a_sql, b_sql):
        memory.servers[server.name] = server
    models = _models(a_sql, b_sql)
    await memory.create_models(models, audit=[SEED] * len(models))

    for make in QUERIES:
        query = make(a_sql, b_sql)
        sql = [m.slug for m in await sql_repo.list_models(query)]
        mem = [m.slug for m in await memory.list_models(query)]
        assert sql == mem, query


@pytest.mark.asyncio
async def test_reads_round_trip_and_defaults(sql_repo):
    a, b = await _seed(sql_repo)
    assert a.created_at is not None  # database default returned
    assert (await sql_repo.get_provider_server_by_id(a.id)).name == "a"
    assert set(await sql_repo.get_provider_servers_by_ids({a.id, b.id})) == {a.id, b.id}
    assert [s.name for s in await sql_repo.list_provider_servers()] == ["a", "b"]
    assert [s.name for s in await sql_repo.list_provider_servers("u2")] == ["b"]

    p1 = await sql_repo.get_model(a.id, "p1")
    assert p1.exec_state == "published"
    assert p1.applications == [{"title": {"de": "A"}}]
    assert p1.io_notes == {"in": {"de": "m"}}
    assert (await sql_repo.get_model_by_id(p1.id)).slug == "a-p1"

    assert await sql_repo.list_published_process_ids() == [
        ("a", "p1"),
        ("b", "p4"),
        ("b", "p5"),
    ]


@pytest.mark.asyncio
async def test_upsert_user_keeps_claims_and_returns_timestamps(sql_repo):
    first = await sql_repo.upsert_user(AppUser(sub="x", display_name="X", email="x@e"))
    assert first.first_seen_at is not None and first.last_seen_at is not None
    again = await sql_repo.upsert_user(AppUser(sub="x"))  # token without claims
    assert again.display_name == "X" and again.email == "x@e"
    assert again.first_seen_at == first.first_seen_at
