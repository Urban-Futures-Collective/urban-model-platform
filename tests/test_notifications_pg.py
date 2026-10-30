"""Notifications against real PostgreSQL (REF-F12 phase E2): migration 0005,
the SQL audit log as outbox (list_after, cursor), the account cache columns,
and the dispatcher end to end on the SQL adapters.

Runs only with UMP_TEST_DATABASE_URL (see test_migration_0003_registry.py);
upgrades and downgrades that database, so never point it at real data.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ump.adapters.audit_log_sql import SQLAuditLog
from ump.adapters.registry_repository_sql import SQLModelRegistryRepository
from ump.core.interfaces.audit_log import NotificationCursor
from ump.core.interfaces.mailer import MailerPort
from ump.core.interfaces.notification_renderer import (
    NotificationRendererPort,
    RenderedMail,
)
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.audit import AuditEvent
from ump.core.services.notifications import NotificationDispatcher

DB_URL = os.environ.get("UMP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="UMP_TEST_DATABASE_URL not set (needs PostgreSQL)"
)
REPO_ROOT = Path(__file__).resolve().parents[1]
TRUNCATE = (
    "TRUNCATE registry.access_request, registry.model, registry.provider_server, "
    "registry.app_user, registry.audit_log"
)


def _alembic(*args: str) -> None:
    from alembic import command
    from alembic.config import Config

    os.environ["UMP_DATABASE_URL"] = DB_URL
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    getattr(command, args[0])(cfg, *args[1:])


def _sql(*statements: str):
    import asyncpg

    async def go():
        conn = await asyncpg.connect(DB_URL.replace("postgresql+asyncpg", "postgresql"))
        try:
            result = None
            for stmt in statements:
                result = await conn.fetch(stmt)
            return result
        finally:
            await conn.close()

    return asyncio.run(go())


def test_migration_seeds_the_cursor_at_the_end_of_the_log():
    _alembic("upgrade", "head")
    _sql(TRUNCATE)
    _alembic("downgrade", "0004")
    try:
        _sql(
            "INSERT INTO registry.audit_log (action) "
            "SELECT 'old.event' FROM generate_series(1, 3)"
        )
        last = _sql("SELECT max(id) AS m FROM registry.audit_log")[0]["m"]
        _alembic("upgrade", "head")
        (row,) = _sql("SELECT * FROM registry.notification_cursor")
        assert (row["id"], row["last_audit_id"], row["attempts"]) == (1, last, 0)
        cols = _sql(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_schema = 'registry' AND table_name = 'app_user'"
        )
        assert {"platform_roles", "lang", "notify"} <= {c["column_name"] for c in cols}

        import asyncpg

        for bad in (
            "INSERT INTO registry.notification_cursor (id, last_audit_id) "
            "VALUES (2, 0)",
            "INSERT INTO registry.app_user (sub, notify) VALUES ('x', '[]')",
        ):
            with pytest.raises(asyncpg.PostgresError):
                _sql(bad)
    finally:
        _sql(TRUNCATE)
        _alembic("upgrade", "head")


def test_downgrade_removes_the_notification_schema():
    _alembic("upgrade", "head")
    try:
        _alembic("downgrade", "0004")
        tables = _sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'registry'"
        )
        assert "notification_cursor" not in {t["table_name"] for t in tables}
        cols = _sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'registry' AND table_name = 'app_user'"
        )
        assert "notify" not in {c["column_name"] for c in cols}
    finally:
        _alembic("upgrade", "head")


@pytest.fixture
def migrated():
    _alembic("upgrade", "head")  # alembic runs its own event loop: keep it sync


@pytest_asyncio.fixture
async def db(migrated):
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.execute(text(TRUNCATE))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    log = SQLAuditLog(sessions)
    await log.save_notification_cursor(NotificationCursor(last_audit_id=0))
    yield SQLModelRegistryRepository(sessions), log
    async with engine.begin() as conn:
        await conn.execute(text(TRUNCATE))
    await engine.dispose()


@pytest.mark.asyncio
async def test_list_after_and_cursor(db):
    _, log = db
    for i in range(3):
        await log.append(AuditEvent(actor_sub=None, action=f"e.{i}"))
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    events = await log.list_after(0, later, limit=10)
    assert [e.action for e in events] == ["e.0", "e.1", "e.2"]  # oldest first
    assert [e.action for e in await log.list_after(events[0].id, later, 1)] == ["e.1"]
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert await log.list_after(0, past, 10) == []

    cursor = NotificationCursor(
        last_audit_id=events[1].id, failing_audit_id=9, attempts=2
    )
    await log.save_notification_cursor(cursor)
    assert await log.get_notification_cursor() == cursor


@pytest.mark.asyncio
async def test_account_cache_columns(db):
    repo, _ = db
    stored = await repo.upsert_user(
        AppUser(sub="u", email="u@x.org", platform_roles=["verifier"], lang="de")
    )
    assert stored.platform_roles == ["verifier"] and stored.lang == "de"
    assert stored.notify == {}

    kept = await repo.upsert_user(AppUser(sub="u"))  # no claims: nothing cleared
    assert kept.platform_roles == ["verifier"] and kept.lang == "de"
    lost = await repo.upsert_user(AppUser(sub="u", platform_roles=[]))
    assert lost.platform_roles == []

    await repo.upsert_user(AppUser(sub="v", platform_roles=["verifier", "user"]))
    await repo.upsert_user(AppUser(sub="w", platform_roles=["user"]))
    assert [u.sub for u in await repo.list_users_with_role("verifier")] == ["v"]

    updated = await repo.set_notify("w", {"jobs": True})
    assert updated.notify == {"jobs": True}
    with pytest.raises(KeyError):
        await repo.set_notify("ghost", {})

    created = await repo.ensure_user(AppUser(sub="new"))
    assert created.platform_roles == [] and created.notify == {}


class Mailer(MailerPort):
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class Renderer(NotificationRendererPort):
    def render(self, kind, lang, params):
        return RenderedMail(subject=f"{kind}:{params['model']['name']}", text="")


@pytest.mark.asyncio
async def test_registry_change_reaches_the_mailer(db):
    repo, log = db
    await repo.upsert_user(AppUser(sub="owner", platform_roles=["provider"]))
    await repo.upsert_user(
        AppUser(sub="ver", email="ver@x.org", platform_roles=["verifier"])
    )
    server = await repo.create_provider_server(
        ProviderServer(name="srv", base_url="https://srv/"),
        audit=AuditEvent(actor_sub="owner", action="server.create"),
    )
    (model,) = await repo.create_models(
        [
            RegistryModel(
                slug="srv-echo",
                provider_server_id=server.id,
                remote_process_id="echo",
                owner_sub="owner",
                full_name={"en": "Echo"},
                purpose={"en": "x"},
                license="MIT",
            )
        ],
        audit=[AuditEvent(actor_sub="owner", action="model.create")],
    )
    # the change and its audit row in one transaction, as the services do it
    await repo.update_model(
        model.model_copy(update={"status": "submitted"}),
        expected_updated_at=model.updated_at,
        audit=AuditEvent(
            actor_sub="owner",
            action="model.status.change",
            model_id=model.id,
            details={"from": "draft", "to": "submitted"},
        ),
    )
    mailer = Mailer()
    dispatcher = NotificationDispatcher(
        log,
        repo,
        Renderer(),
        mailer,
        languages=["en", "de"],
        settle_seconds=0,
        clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    result = await dispatcher.run_once()
    assert result.events == 3  # server.create and model.create mail nobody
    assert [(m.to, m.subject) for m in mailer.sent] == [
        ("ver@x.org", "model_submitted:Echo")
    ]
    assert (await dispatcher.run_once()).events == 0
