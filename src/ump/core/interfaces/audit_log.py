"""AuditLogPort — the append-only audit log (REF-F12 "Audit log").

Registry changes are *not* written through this port: the repository write
that makes a change stores its audit row in the same transaction (see
``ModelRegistryPort``). This port is for events that change nothing in the
registry — job executions (``AuditObserver``) — for reading the log
(``GET /registry/v1/audit``), and for reading it as the outbox of email
notifications, with the dispatcher's cursor.
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


@dataclass(frozen=True)
class NotificationCursor:
    """How far the notification dispatcher has read the log (migration 0005).

    The log is append-only, so delivery state cannot be a flag on its rows.
    ``failing_audit_id``/``attempts`` track the event being retried after a
    transient mail error.
    """

    last_audit_id: int = 0
    failing_audit_id: int | None = None
    attempts: int = 0


class AuditLogPort(ABC):
    @abstractmethod
    async def append(self, event: AuditEvent) -> None:
        """Store *event* on its own (no registry change goes with it)."""

    @abstractmethod
    async def list_events(self, query: AuditQuery) -> list[AuditRecord]:
        pass

    # --- the log as notification outbox (REF-F12 "Notifications") ---------

    @abstractmethod
    async def list_after(
        self, after_id: int, occurred_before: datetime, limit: int
    ) -> list[AuditRecord]:
        """Events with ``id > after_id`` and ``occurred_at < occurred_before``,
        oldest first. The time bound skips rows of transactions that may still
        be followed by a lower id committing late."""

    @abstractmethod
    async def get_notification_cursor(self) -> NotificationCursor:
        pass

    @abstractmethod
    async def save_notification_cursor(self, cursor: NotificationCursor) -> None:
        pass
