"""SQLModel-backed implementation of ModelRegistryPort (schema `registry`).

Same two-model pattern as job_repository_sql.py:
  - Core domain models: ump.core.models.registry (pure Pydantic, no ORM)
  - Adapter ORM models: AppUserRecord / ProviderServerRecord / ModelRecord

The tables are created by migration 0003. ``exec_state`` is a generated column
and not mapped (the core derives it, see ``exec_state_for``). Columns with
database defaults (timestamps) are left out of INSERTs when the domain value is
None, and every write returns the stored row.

The session factory is injected by the composition root.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import Column, DateTime, LargeBinary, func, or_
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import Field, SQLModel, select

from ump.core.interfaces.model_registry import ModelQuery, ModelRegistryPort
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel

SCHEMA = "registry"

# Columns whose value the database supplies when the domain model has none.
_DB_DEFAULTED = {"first_seen_at", "last_seen_at", "created_at", "updated_at"}


def _tz(nullable: bool = True) -> Any:
    return Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=nullable)
    )


def _jsonb(nullable: bool = False) -> Any:
    return Field(default=None, sa_column=Column(JSONB, nullable=nullable))


def _insert_values(domain: Any) -> dict[str, Any]:
    return {
        k: v
        for k, v in domain.model_dump().items()
        if not (k in _DB_DEFAULTED and v is None)
    }


class AppUserRecord(SQLModel, table=True):
    __tablename__: str = "app_user"
    __table_args__ = {"schema": SCHEMA}

    sub: str = Field(primary_key=True)
    display_name: Optional[str] = None
    email: Optional[str] = None
    first_seen_at: Optional[datetime] = _tz()
    last_seen_at: Optional[datetime] = _tz()


class ProviderServerRecord(SQLModel, table=True):
    __tablename__: str = "provider_server"
    __table_args__ = {"schema": SCHEMA}

    id: UUID = Field(primary_key=True)
    name: str
    base_url: str
    owner_sub: Optional[str] = None
    auth_type: str
    credentials: Optional[bytes] = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    ttw_job_done_seconds: float
    is_active: bool
    last_discovered_at: Optional[datetime] = _tz()
    created_at: Optional[datetime] = _tz()
    updated_at: Optional[datetime] = _tz()


class ModelRecord(SQLModel, table=True):
    __tablename__: str = "model"
    __table_args__ = {"schema": SCHEMA}

    id: UUID = Field(primary_key=True)
    slug: str
    provider_server_id: UUID
    remote_process_id: str
    owner_sub: str
    default_lang: str
    full_name: dict = _jsonb()
    short_description: dict = _jsonb()
    purpose: dict = _jsonb()
    limitations_risks: dict = _jsonb()
    keywords: dict = _jsonb()
    version_label: Optional[str] = None
    repository_url: Optional[str] = None
    code_ref: Optional[str] = None
    license: Optional[str] = None
    requirements: dict = _jsonb()
    contributors: list = _jsonb()
    literature: list = _jsonb()
    applications: list = _jsonb()
    test_cases: list = _jsonb()
    io_notes: dict = _jsonb()
    process_config: dict = _jsonb()
    status: str
    status_note: Optional[str] = None
    status_changed_by: Optional[str] = None
    status_changed_at: Optional[datetime] = _tz()
    published_at: Optional[datetime] = _tz()
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = _tz()
    verification_job_id: Optional[str] = None
    verified_interface: Optional[dict] = _jsonb(nullable=True)
    verified_interface_sha256: Optional[str] = None
    current_interface_sha256: Optional[str] = None
    interface_state: str
    last_checked_at: Optional[datetime] = _tz()
    created_at: Optional[datetime] = _tz()
    updated_at: Optional[datetime] = _tz()


_USERS = AppUserRecord.__table__
_SERVERS = ProviderServerRecord.__table__
_MODELS = ModelRecord.__table__


def _user(row: Any) -> AppUser:
    return AppUser.model_validate(dict(row._mapping))


def _server(row: Any) -> ProviderServer:
    return ProviderServer.model_validate(dict(row._mapping))


def _model(row: Any) -> RegistryModel:
    return RegistryModel.model_validate(dict(row._mapping))


class SQLModelRegistryRepository(ModelRegistryPort):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _one(self, stmt: Any, commit: bool = False) -> Any:
        async with self._session_factory() as session:
            row = (await session.execute(stmt)).first()
            if commit:
                await session.commit()
            return row

    async def _all(self, stmt: Any) -> list[Any]:
        async with self._session_factory() as session:
            return list((await session.execute(stmt)).all())

    # --- accounts ---------------------------------------------------------

    async def upsert_user(self, user: AppUser) -> AppUser:
        values = _insert_values(user)
        stmt = insert(_USERS).values(**values)
        refreshed = {
            k: stmt.excluded[k]
            for k in ("display_name", "email")
            if values.get(k)  # never overwrite a cached claim with nothing
        }
        refreshed["last_seen_at"] = func.now()
        stmt = stmt.on_conflict_do_update(
            index_elements=["sub"], set_=refreshed
        ).returning(*_USERS.c)
        return _user(await self._one(stmt, commit=True))

    # --- provider servers -------------------------------------------------

    async def get_provider_server(self, name: str) -> ProviderServer | None:
        row = await self._one(select(*_SERVERS.c).where(_SERVERS.c.name == name))
        return _server(row) if row else None

    async def get_provider_server_by_id(self, server_id: UUID) -> ProviderServer | None:
        row = await self._one(select(*_SERVERS.c).where(_SERVERS.c.id == server_id))
        return _server(row) if row else None

    async def get_provider_servers_by_ids(
        self, server_ids: set[UUID]
    ) -> dict[UUID, ProviderServer]:
        if not server_ids:
            return {}
        rows = await self._all(select(*_SERVERS.c).where(_SERVERS.c.id.in_(server_ids)))
        return {s.id: s for s in map(_server, rows)}

    async def list_provider_servers(
        self, owner_sub: str | None = None
    ) -> list[ProviderServer]:
        stmt = select(*_SERVERS.c).order_by(_SERVERS.c.name)
        if owner_sub is not None:
            stmt = stmt.where(_SERVERS.c.owner_sub == owner_sub)
        return [_server(r) for r in await self._all(stmt)]

    async def create_provider_server(self, server: ProviderServer) -> ProviderServer:
        stmt = insert(_SERVERS).values(**_insert_values(server)).returning(*_SERVERS.c)
        return _server(await self._one(stmt, commit=True))

    # --- models -----------------------------------------------------------

    async def get_model(
        self, provider_server_id: UUID, remote_process_id: str
    ) -> RegistryModel | None:
        row = await self._one(
            select(*_MODELS.c).where(
                _MODELS.c.provider_server_id == provider_server_id,
                _MODELS.c.remote_process_id == remote_process_id,
            )
        )
        return _model(row) if row else None

    async def get_model_by_id(self, model_id: UUID) -> RegistryModel | None:
        row = await self._one(select(*_MODELS.c).where(_MODELS.c.id == model_id))
        return _model(row) if row else None

    async def list_models(self, query: ModelQuery) -> list[RegistryModel]:
        c = _MODELS.c
        stmt = select(*c).order_by(c.slug).limit(query.limit)
        if query.visible_statuses is not None:
            visible = c.status.in_(query.visible_statuses)
            if query.visible_owner is not None:
                visible = or_(visible, c.owner_sub == query.visible_owner)
            stmt = stmt.where(visible)
        if query.status is not None:
            stmt = stmt.where(c.status == query.status)
        if query.owner_sub is not None:
            stmt = stmt.where(c.owner_sub == query.owner_sub)
        if query.provider_server_id is not None:
            stmt = stmt.where(c.provider_server_id == query.provider_server_id)
        if query.after_slug is not None:
            stmt = stmt.where(c.slug > query.after_slug)
        return [_model(r) for r in await self._all(stmt)]

    async def list_published_process_ids(self) -> list[tuple[str, str]]:
        rows = await self._all(
            select(_SERVERS.c.name, _MODELS.c.remote_process_id)
            .join(_SERVERS, _SERVERS.c.id == _MODELS.c.provider_server_id)
            .where(_MODELS.c.published_at.is_not(None))
            .order_by(_SERVERS.c.name, _MODELS.c.remote_process_id)
        )
        return [(r[0], r[1]) for r in rows]

    async def create_model(self, model: RegistryModel) -> RegistryModel:
        stmt = insert(_MODELS).values(**_insert_values(model)).returning(*_MODELS.c)
        return _model(await self._one(stmt, commit=True))
