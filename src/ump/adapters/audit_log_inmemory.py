"""In-memory AuditLogPort for tests and database-less development.

Numbers events in insertion order and stamps ``occurred_at``, like the
``bigserial`` id and the ``now()`` default of ``registry.audit_log``. The
in-memory registry repository writes its audit rows here too, when it is given
this log, so ``GET /audit`` sees registry changes without a database.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ump.core.interfaces.audit_log import (
    AuditLogPort,
    AuditQuery,
    NotificationCursor,
)
from ump.core.models.registry.audit import AuditEvent, AuditRecord


class InMemoryAuditLog(AuditLogPort):
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        self.cursor = NotificationCursor()

    async def append(self, event: AuditEvent) -> None:
        self.records.append(
            AuditRecord(
                **event.model_dump(),
                id=len(self.records) + 1,
                occurred_at=datetime.now(timezone.utc),
            )
        )

    async def list_events(self, query: AuditQuery) -> list[AuditRecord]:
        matching = [r for r in reversed(self.records) if query.matches(r)]
        return [r.model_copy() for r in matching[: query.limit]]

    async def list_after(
        self, after_id: int, occurred_before: datetime, limit: int
    ) -> list[AuditRecord]:
        matching = [
            r
            for r in self.records
            if r.id > after_id and r.occurred_at < occurred_before
        ]
        return [r.model_copy() for r in matching[:limit]]

    async def get_notification_cursor(self) -> NotificationCursor:
        return self.cursor

    async def save_notification_cursor(self, cursor: NotificationCursor) -> None:
        self.cursor = cursor
