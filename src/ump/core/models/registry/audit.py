"""Audit events (REF-F12 "Audit log").

Services build the event — the core decides what is recorded — and pass it to
the repository write that makes the change; the repository stores both in one
transaction. Secrets never go into ``details``.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from ump.core.interfaces.auth import AuthContext
from ump.core.logging_config import correlation_id_var


class AuditEvent(BaseModel):
    actor_sub: str | None
    actor_roles: list[str] = Field(default_factory=list)  # snapshot from the token
    action: str  # e.g. server.create, model.card.update
    model_id: UUID | None = None
    job_id: str | None = None
    outcome: Literal["success", "denied", "failure"] = "success"
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None

    @classmethod
    def by(cls, auth: AuthContext, action: str, **fields: Any) -> "AuditEvent":
        rid = correlation_id_var.get()
        return cls(
            actor_sub=auth.user_id,
            actor_roles=sorted(set(auth.roles)),
            action=action,
            request_id=None if rid in (None, "-") else rid,
            **fields,
        )


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """``{field: {"before": …, "after": …}}`` for every changed field."""
    return {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
