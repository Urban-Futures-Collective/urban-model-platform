"""In-memory AuditLogPort for tests and database-less development.

Numbers events in insertion order and stamps ``occurred_at``, like the
``bigserial`` id and the ``now()`` default of ``registry.audit_log``. The
in-memory registry repository writes its audit rows here too, when it is given
this log, so ``GET /audit`` sees registry changes without a database.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ump.core.interfaces.audit_log import AuditLogPort, AuditQuery
from ump.core.models.registry.audit import AuditEvent, AuditRecord


class InMemoryAuditLog(AuditLogPort):
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

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
