"""SQLModel-backed AuditLogPort (``registry.audit_log``, migration 0003, and
``registry.notification_cursor``, migration 0005).

The table is append-only: a trigger refuses UPDATE and DELETE outside the
retention transaction. ``AuditLogRecord`` is also used by the registry
repository, which inserts the audit row of a change in the change's own
transaction.

The session factory is injected by the composition root.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import Column, DateTime, Text, func, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import Field, SQLModel, select

from ump.core.interfaces.audit_log import (
    AuditLogPort,
    AuditQuery,
    NotificationCursor,
)
from ump.core.models.registry.audit import AuditEvent, AuditRecord

SCHEMA = "registry"


class AuditLogRecord(SQLModel, table=True):
    """Append-only; ``id`` and ``occurred_at`` come from the database."""

    __tablename__: str = "audit_log"
    __table_args__ = {"schema": SCHEMA}

    id: Optional[int] = Field(default=None, primary_key=True)
    occurred_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    actor_sub: Optional[str] = None
    actor_roles: list = Field(
        default=None, sa_column=Column(ARRAY(Text), nullable=False)
    )
    action: str
    model_id: Optional[UUID] = None
    job_id: Optional[str] = None
    outcome: str
    details: dict = Field(default=None, sa_column=Column(JSONB, nullable=False))
    request_id: Optional[str] = None


AUDIT = AuditLogRecord.__table__


class NotificationCursorRecord(SQLModel, table=True):
    """The single row (``id = 1``) seeded by migration 0005."""

    __tablename__: str = "notification_cursor"
    __table_args__ = {"schema": SCHEMA}

    id: int = Field(primary_key=True)
    last_audit_id: int
    failing_audit_id: Optional[int] = None
    attempts: int
    updated_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


CURSOR = NotificationCursorRecord.__table__


def audit_values(event: AuditEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


class SQLAuditLog(AuditLogPort):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(self, event: AuditEvent) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(insert(AUDIT).values(**audit_values(event)))

    async def list_events(self, query: AuditQuery) -> list[AuditRecord]:
        c = AUDIT.c
        stmt = select(*c).order_by(c.id.desc()).limit(query.limit)
        for column, value in (
            (c.model_id, query.model_id),
            (c.actor_sub, query.actor_sub),
            (c.action, query.action),
            (c.job_id, query.job_id),
        ):
            if value is not None:
                stmt = stmt.where(column == value)
        if query.since is not None:
            stmt = stmt.where(c.occurred_at >= query.since)
        if query.until is not None:
            stmt = stmt.where(c.occurred_at < query.until)
        if query.before_id is not None:
            stmt = stmt.where(c.id < query.before_id)
        return await self._records(stmt)

    async def list_after(
        self, after_id: int, occurred_before: datetime, limit: int
    ) -> list[AuditRecord]:
        c = AUDIT.c
        stmt = (
            select(*c)
            .where(c.id > after_id, c.occurred_at < occurred_before)
            .order_by(c.id)
            .limit(limit)
        )
        return await self._records(stmt)

    async def get_notification_cursor(self) -> NotificationCursor:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        CURSOR.c.last_audit_id,
                        CURSOR.c.failing_audit_id,
                        CURSOR.c.attempts,
                    ).where(CURSOR.c.id == 1)
                )
            ).first()
        if row is None:  # migration 0005 seeds it; never mail old history
            raise RuntimeError("registry.notification_cursor has no row; migrate")
        return NotificationCursor(**row._mapping)

    async def save_notification_cursor(self, cursor: NotificationCursor) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(CURSOR)
                    .where(CURSOR.c.id == 1)
                    .values(
                        last_audit_id=cursor.last_audit_id,
                        failing_audit_id=cursor.failing_audit_id,
                        attempts=cursor.attempts,
                        updated_at=func.now(),
                    )
                )

    async def _records(self, stmt) -> list[AuditRecord]:
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return [AuditRecord.model_validate(dict(r._mapping)) for r in rows]
