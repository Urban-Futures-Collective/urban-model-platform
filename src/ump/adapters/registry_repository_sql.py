"""SQLModel-backed implementation of ModelRegistryPort (schema `registry`).

Same two-model pattern as job_repository_sql.py:
  - Core domain models: ump.core.models.registry (pure Pydantic, no ORM)
  - Adapter ORM models: AppUserRecord / ProviderServerRecord / ModelRecord /
    AccessRequestRecord

The tables are created by migrations 0003 and 0004. ``exec_state`` is a generated column
and not mapped (the core derives it, see ``exec_state_for``). Columns with
database defaults (timestamps) are left out of INSERTs when the domain value is
None, and every write returns the stored row.

Every write and its audit row run in one transaction. Updates are conditional
on ``updated_at`` (optimistic concurrency) and set it to ``now()`` — except
drift-check results, which are conditional on the verified hash and leave
``updated_at`` alone. A unique violation becomes ``RegistryConflictError``.

The session factory is injected by the composition root.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import (
    Column,
    DateTime,
    LargeBinary,
    Text,
    func,
    or_,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import Field, SQLModel, select

from ump.adapters.audit_log_sql import AUDIT, audit_values
from ump.core.interfaces.model_registry import (
    AccessQuery,
    InterfaceCheck,
    ModelQuery,
    ModelRegistryPort,
    RegistryConflictError,
    StaleWriteError,
)
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.access import AccessRequest
from ump.core.models.registry.audit import AuditEvent

SCHEMA = "registry"

# Columns whose value the database supplies when the domain model has none.
_DB_DEFAULTED = {
    "first_seen_at",
    "last_seen_at",
    "created_at",
    "updated_at",
    "status_changed_at",
    "platform_roles",
}


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
    platform_roles: list = Field(
        default=None, sa_column=Column(ARRAY(Text), nullable=False)
    )
    lang: Optional[str] = None
    notify: dict = _jsonb()


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


class AccessRequestRecord(SQLModel, table=True):
    __tablename__: str = "access_request"
    __table_args__ = {"schema": SCHEMA}

    id: UUID = Field(primary_key=True)
    user_sub: str
    model_id: UUID
    status: str
    request_note: Optional[str] = None
    status_note: Optional[str] = None
    status_changed_by: Optional[str] = None
    status_changed_at: Optional[datetime] = _tz()
    created_at: Optional[datetime] = _tz()
    updated_at: Optional[datetime] = _tz()


_USERS = AppUserRecord.__table__
_UNIQUE_VIOLATION = "23505"

# never rewritten by an update
_SERVER_IDENTITY = {"id", "name", "created_at", "updated_at"}
_MODEL_IDENTITY = {
    "id",
    "provider_server_id",
    "remote_process_id",
    "created_at",
    "updated_at",
}


def _is_unique_violation(exc: IntegrityError) -> bool:
    code = getattr(exc.orig, "pgcode", None) or getattr(exc.orig, "sqlstate", None)
    return code == _UNIQUE_VIOLATION


_SERVERS = ProviderServerRecord.__table__
_MODELS = ModelRecord.__table__
_ACCESS = AccessRequestRecord.__table__

# the only columns a step of the access workflow changes
_ACCESS_STEP = ("status", "status_note", "status_changed_by", "status_changed_at")


def _user(row: Any) -> AppUser:
    return AppUser.model_validate(dict(row._mapping))


def _server(row: Any) -> ProviderServer:
    return ProviderServer.model_validate(dict(row._mapping))


def _model(row: Any) -> RegistryModel:
    return RegistryModel.model_validate(dict(row._mapping))


def _access(row: Any) -> AccessRequest:
    return AccessRequest.model_validate(dict(row._mapping))


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

    async def _write(self, stmts: list[Any], audits: list[AuditEvent]) -> list[Any]:
        """Run *stmts* and insert *audits* in one transaction; first row of each."""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    rows = [(await session.execute(stmt)).first() for stmt in stmts]
                    if audits:
                        await session.execute(
                            insert(AUDIT), [audit_values(a) for a in audits]
                        )
                    if any(row is None for row in rows):
                        raise StaleWriteError("row changed or missing")
                    return rows
        except IntegrityError as exc:
            if _is_unique_violation(exc):
                raise RegistryConflictError(str(exc.orig)) from exc
            raise

    # --- accounts ---------------------------------------------------------

    async def upsert_user(self, user: AppUser) -> AppUser:
        values = _insert_values(user)
        if user.platform_roles is not None:
            values["platform_roles"] = sorted(user.platform_roles)
        stmt = insert(_USERS).values(**values)
        refreshed = {
            k: stmt.excluded[k]
            for k in ("display_name", "email", "lang", "platform_roles")
            # never overwrite a cached claim with nothing (an empty role list
            # is a claim: the user lost their roles)
            if values.get(k) or (k == "platform_roles" and k in values)
        }
        refreshed["last_seen_at"] = func.now()
        stmt = stmt.on_conflict_do_update(
            index_elements=["sub"], set_=refreshed
        ).returning(*_USERS.c)
        return _user(await self._one(stmt, commit=True))

    async def ensure_user(self, user: AppUser) -> AppUser:
        inserted = await self._one(
            insert(_USERS)
            .values(**_insert_values(user))
            .on_conflict_do_nothing(index_elements=["sub"])
            .returning(*_USERS.c),
            commit=True,
        )
        if inserted is None:  # known already: keep the row as it is
            inserted = await self._one(
                select(*_USERS.c).where(_USERS.c.sub == user.sub)
            )
        return _user(inserted)

    async def get_users(self, subs: set[str]) -> dict[str, AppUser]:
        if not subs:
            return {}
        rows = await self._all(select(*_USERS.c).where(_USERS.c.sub.in_(subs)))
        return {u.sub: u for u in map(_user, rows)}

    async def list_users_with_role(self, role: str) -> list[AppUser]:
        rows = await self._all(
            select(*_USERS.c)
            .where(_USERS.c.platform_roles.any(role))
            .order_by(_USERS.c.sub)
        )
        return [_user(r) for r in rows]

    async def set_notify(self, sub: str, notify: dict[str, bool]) -> AppUser:
        row = await self._one(
            update(_USERS)
            .where(_USERS.c.sub == sub)
            .values(notify=notify)
            .returning(*_USERS.c),
            commit=True,
        )
        if row is None:
            raise KeyError(sub)
        return _user(row)

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

    async def create_provider_server(
        self, server: ProviderServer, *, audit: AuditEvent
    ) -> ProviderServer:
        stmt = insert(_SERVERS).values(**_insert_values(server)).returning(*_SERVERS.c)
        (row,) = await self._write([stmt], [audit])
        return _server(row)

    async def update_provider_server(
        self, server: ProviderServer, *, expected_updated_at, audit: AuditEvent
    ) -> ProviderServer:
        values = server.model_dump(exclude=_SERVER_IDENTITY)
        stmt = (
            update(_SERVERS)
            .where(
                _SERVERS.c.id == server.id,
                _SERVERS.c.updated_at == expected_updated_at,
            )
            .values(**values, updated_at=func.now())
            .returning(*_SERVERS.c)
        )
        (row,) = await self._write([stmt], [audit])
        return _server(row)

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

    async def get_model_by_slug(self, slug: str) -> RegistryModel | None:
        row = await self._one(select(*_MODELS.c).where(_MODELS.c.slug == slug))
        return _model(row) if row else None

    async def create_models(
        self, models: list[RegistryModel], *, audit: list[AuditEvent]
    ) -> list[RegistryModel]:
        stmts = [
            insert(_MODELS).values(**_insert_values(m)).returning(*_MODELS.c)
            for m in models
        ]
        return [_model(row) for row in await self._write(stmts, audit)]

    async def update_model(
        self, model: RegistryModel, *, expected_updated_at, audit: AuditEvent
    ) -> RegistryModel:
        values = model.model_dump(exclude=_MODEL_IDENTITY)
        stmt = (
            update(_MODELS)
            .where(
                _MODELS.c.id == model.id,
                _MODELS.c.updated_at == expected_updated_at,
            )
            .values(**values, updated_at=func.now())
            .returning(*_MODELS.c)
        )
        (row,) = await self._write([stmt], [audit])
        return _model(row)

    async def record_interface_checks(
        self, checks: list[InterfaceCheck], *, audit: list[AuditEvent] = ()
    ) -> set[UUID]:
        # one transaction, so the providers NOTIFY trigger fires once per run
        applied: set[UUID] = set()
        events = list(audit)
        async with self._session_factory() as session:
            async with session.begin():
                for check in checks:
                    stmt = (
                        update(_MODELS)
                        .where(
                            _MODELS.c.id == check.model_id,
                            _MODELS.c.verified_interface_sha256.is_not_distinct_from(
                                check.expected_verified_sha256
                            ),
                        )
                        .values(
                            interface_state=check.interface_state,
                            current_interface_sha256=check.current_interface_sha256,
                            last_checked_at=check.last_checked_at,
                        )
                        .returning(_MODELS.c.id)
                    )
                    if (await session.execute(stmt)).first() is None:
                        continue
                    applied.add(check.model_id)
                    if check.audit is not None:
                        events.append(check.audit)
                if events:
                    await session.execute(
                        insert(AUDIT), [audit_values(e) for e in events]
                    )
        return applied

    # --- access requests --------------------------------------------------

    async def get_access_request(self, request_id: UUID) -> AccessRequest | None:
        row = await self._one(select(*_ACCESS.c).where(_ACCESS.c.id == request_id))
        return _access(row) if row else None

    async def list_access_requests(self, query: AccessQuery) -> list[AccessRequest]:
        c = _ACCESS.c
        stmt = select(*c).order_by(c.created_at.desc(), c.id.desc()).limit(query.limit)
        if query.visible_to is not None:
            owned = select(_MODELS.c.id).where(_MODELS.c.owner_sub == query.visible_to)
            stmt = stmt.where(
                or_(c.user_sub == query.visible_to, c.model_id.in_(owned))
            )
        if query.status is not None:
            stmt = stmt.where(c.status == query.status)
        if query.model_id is not None:
            stmt = stmt.where(c.model_id == query.model_id)
        if query.user_sub is not None:
            stmt = stmt.where(c.user_sub == query.user_sub)
        if query.before is not None:
            stmt = stmt.where(tuple_(c.created_at, c.id) < tuple_(*query.before))
        return [_access(r) for r in await self._all(stmt)]

    async def create_access_request(
        self, request: AccessRequest, *, audit: AuditEvent
    ) -> AccessRequest:
        stmt = insert(_ACCESS).values(**_insert_values(request)).returning(*_ACCESS.c)
        (row,) = await self._write([stmt], [audit])
        return _access(row)

    async def update_access_request(
        self, request: AccessRequest, *, expected_updated_at, audit: AuditEvent
    ) -> AccessRequest:
        stmt = (
            update(_ACCESS)
            .where(
                _ACCESS.c.id == request.id,
                _ACCESS.c.updated_at == expected_updated_at,
            )
            .values(
                **request.model_dump(include=set(_ACCESS_STEP)), updated_at=func.now()
            )
            .returning(*_ACCESS.c)
        )
        (row,) = await self._write([stmt], [audit])
        return _access(row)
