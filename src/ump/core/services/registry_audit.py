"""RegistryAuditService — reading the audit log (REF-F12 phase D).

For ``verifier``, ``access-admin`` and ``platform-admin`` ("Authorization").
Newest first, keyset-paginated by event id. The log holds three months
(D11); older events are gone once retention has run.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from ump.core.interfaces.audit_log import AuditLogPort, AuditQuery
from ump.core.interfaces.auth import AuthContext
from ump.core.models.registry import roles
from ump.core.models.registry.views import AuditPage
from ump.core.services.authorization import AuthorizationService
from ump.core.services.registry_common import problem

MAX_PAGE_SIZE = 200
AUDIT_READERS = (roles.VERIFIER, roles.ACCESS_ADMIN, roles.PLATFORM_ADMIN)


class RegistryAuditService:
    def __init__(
        self, audit_log: AuditLogPort, authorization: AuthorizationService
    ) -> None:
        self._log = audit_log
        self._authz = authorization

    async def list_events(
        self,
        auth: AuthContext,
        *,
        model_id: UUID | None = None,
        actor: str | None = None,
        action: str | None = None,
        job_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> AuditPage:
        self._authz.require_platform_role(auth, *AUDIT_READERS)
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        records = await self._log.list_events(
            AuditQuery(
                model_id=model_id,
                actor_sub=actor,
                action=action,
                job_id=job_id,
                since=since,
                until=until,
                before_id=_decode_cursor(cursor) if cursor else None,
                limit=limit + 1,  # one more tells whether a next page exists
            )
        )
        page = records[:limit]
        return AuditPage(
            events=page,
            next_cursor=str(page[-1].id) if len(records) > limit else None,
        )


def _decode_cursor(cursor: str) -> int:
    if not cursor.isdigit():
        raise problem(400, "Bad Request", "Invalid cursor.")
    return int(cursor)
