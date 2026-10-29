"""Access requests (REF-F12 "Access requests", D15) — pure domain logic.

    requested ──► granted ──► revoked
        ├──────► denied
        └──────► withdrawn

A record of a user asking for access to a model and an access admin deciding.
It never grants anything: the grant is the Keycloak role ``{provider}:{process}``,
which UMP-X maps *before* recording ``granted`` here (UMP has no IdP write path,
REF-F11). Execution keeps reading roles from the token only.

Who may take a step:

- ``requester``: the user the record is about;
- ``decider``: an ``access-admin`` who is **not** that user (CHECK
  ``ck_access_request_not_self``);
- ``access_admin``: any ``access-admin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from ump.core.interfaces.auth import AuthContext
from ump.core.models.registry import roles

AccessStatus = Literal["requested", "granted", "denied", "withdrawn", "revoked"]
ACCESS_STATUSES = frozenset({"requested", "granted", "denied", "withdrawn", "revoked"})
OPEN_STATUSES = frozenset({"requested", "granted"})  # uq_access_request_open

Actor = Literal["requester", "decider", "access_admin"]


class AccessRequest(BaseModel):
    """One row of ``registry.access_request`` (migration 0004)."""

    id: UUID = Field(default_factory=uuid4)
    user_sub: str
    model_id: UUID
    status: AccessStatus = "requested"
    request_note: str | None = None  # None for a direct grant
    status_note: str | None = None
    status_changed_by: str | None = None
    # set by the database; None until stored
    status_changed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Step:
    actor: Actor
    action: str  # audit action
    needs_note: bool = False


TRANSITIONS: dict[tuple[str, str], Step] = {
    ("requested", "withdrawn"): Step("requester", "access.withdraw"),
    ("requested", "granted"): Step("decider", "access.grant"),
    ("requested", "denied"): Step("decider", "access.deny", needs_note=True),
    ("granted", "revoked"): Step("access_admin", "access.revoke", needs_note=True),
}

# Model statuses in which access can be granted; requests need it too.
GRANTABLE_MODEL_STATUS = "published"


class AccessTransitionError(Exception):
    """The step is not in the graph (409) or lacks something it needs (400)."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class AccessTransitionForbidden(Exception):
    """The caller may not take this step (403)."""


def is_access_admin(auth: AuthContext) -> bool:
    return auth.is_authenticated and roles.ACCESS_ADMIN in auth.roles


def may_act(actor: Actor, request: AccessRequest, auth: AuthContext) -> bool:
    if not auth.is_authenticated:
        return False
    is_requester = request.user_sub == auth.user_id
    if actor == "requester":
        return is_requester
    if actor == "decider":
        return is_access_admin(auth) and not is_requester
    return is_access_admin(auth)


def check_transition(
    request: AccessRequest, to_status: str, auth: AuthContext, note: str | None
) -> Step:
    """The step for ``request.status → to_status`` if *auth* may take it now."""
    step = TRANSITIONS.get((request.status, to_status))
    if step is None:
        allowed = sorted(to for (frm, to) in TRANSITIONS if frm == request.status)
        raise AccessTransitionError(
            f"'{request.status}' cannot move to '{to_status}'"
            + (f"; allowed: {', '.join(allowed)}" if allowed else "")
        )
    if not may_act(step.actor, request, auth):
        who = {
            "requester": "the user who requested access",
            "decider": "an access admin other than the requesting user",
            "access_admin": "an access admin",
        }[step.actor]
        raise AccessTransitionForbidden(
            f"'{request.status}' → '{to_status}' is for {who}."
        )
    if step.needs_note and not (note and note.strip()):
        raise AccessTransitionError(f"'{to_status}' needs a note explaining why.", 400)
    return step


def holds_access(auth: AuthContext, provider: str, process_role: str) -> bool:
    """Whether the token already carries a model-access role for the model."""
    held = set(auth.roles) if auth.is_authenticated else set()
    return provider in held or process_role in held
