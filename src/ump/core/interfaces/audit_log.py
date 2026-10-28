"""AuditLogPort — the append-only audit log (REF-F12 "Audit log").

Registry changes are *not* written through this port: the repository write
that makes a change stores its audit row in the same transaction (see
``ModelRegistryPort``). This port is for events that change nothing in the
registry — job executions (``AuditObserver``) — and for reading the log
(``GET /registry/v1/audit``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ump.core.models.registry.audit import AuditEvent, AuditRecord


@dataclass(frozen=True)
class AuditQuery:
    """Filter for ``list_events``. Results are ordered newest first (by id).

    An event matches when it passes every set filter; ``before_id`` is the
    keyset cursor (ids are increasing in insertion order).
    """

    model_id: UUID | None = None
    actor_sub: str | None = None
    action: str | None = None
    job_id: str | None = None
    since: datetime | None = None  # inclusive
    until: datetime | None = None  # exclusive
    before_id: int | None = None
    limit: int = 50

    def matches(self, record: AuditRecord) -> bool:
        """Reference semantics, used by the in-memory adapter and tests."""
        return (
            (self.model_id is None or record.model_id == self.model_id)
            and (self.actor_sub is None or record.actor_sub == self.actor_sub)
            and (self.action is None or record.action == self.action)
            and (self.job_id is None or record.job_id == self.job_id)
            and (self.since is None or record.occurred_at >= self.since)
            and (self.until is None or record.occurred_at < self.until)
            and (self.before_id is None or record.id < self.before_id)
        )


class AuditLogPort(ABC):
    @abstractmethod
    async def append(self, event: AuditEvent) -> None:
        """Store *event* on its own (no registry change goes with it)."""

    @abstractmethod
    async def list_events(self, query: AuditQuery) -> list[AuditRecord]:
        pass
