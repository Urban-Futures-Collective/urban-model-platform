"""Email notifications (REF-F12 "Notifications", D14) — pure domain logic.

The audit log is the outbox: every change worth a mail already writes an audit
row in the change's own transaction. ``plan`` maps one audit record to the
mails it causes — the catalogue below — without touching any port. Recipients
are *targets* (a user, everyone holding a platform role, or a plain address);
the dispatcher resolves them at send time.

    model.status.change → submitted           review    all verifiers
    model.status.change → any other status    review    owner
        (except submitted → draft, a withdrawal nobody else has seen yet)
    model.reverify                            review    owner
    model.interface.drifted / .unreachable    drift     owner + all verifiers
    model.interface.restored                  drift     owner
    model.owner.transfer                      ownership previous and new owner
    access.request                            access    all access admins
                                                        (+ fallback mailboxes)
                                                        and the model owner
    access.grant / .deny / .revoke            access    the user
    job.complete                              jobs      the job's owner (opt-in)

Rules: the actor is never told about their own action, and only successful
events notify — except ``job.complete``, which is the system telling the job's
owner (its actor) about the outcome, failed or not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Sequence
from uuid import UUID

from ump.core.models.registry import roles
from ump.core.models.registry.audit import AuditRecord

Category = Literal["review", "access", "drift", "ownership", "jobs"]

# What a user gets when they have not chosen (``app_user.notify``).
DEFAULT_PREFERENCES: dict[str, bool] = {
    "review": True,
    "access": True,
    "drift": True,
    "ownership": True,
    "jobs": False,  # one mail per job is too much unless asked for
}
CATEGORIES = frozenset(DEFAULT_PREFERENCES)

# Every template kind the policy can produce; the renderer must know them all.
KINDS = frozenset(
    {
        "model_submitted",
        "model_status",
        "model_reverified",
        "model_interface",
        "model_owner_transfer",
        "access_requested",
        "access_decided",
        "job_complete",
    }
)


def effective_preferences(notify: dict[str, bool]) -> dict[str, bool]:
    """The user's choices over the defaults, for every category."""
    return {
        category: bool(notify.get(category, default))
        for category, default in DEFAULT_PREFERENCES.items()
    }


@dataclass(frozen=True)
class ModelRef:
    """What a mail says about the model an event concerns."""

    id: UUID
    slug: str
    process_id: str  # `{provider}:{process}`, also the model-access role
    owner_sub: str
    full_name: dict[str, str]
    default_lang: str


@dataclass(frozen=True)
class Target:
    """One recipient, resolved by the dispatcher: exactly one field is set."""

    sub: str | None = None  # a user
    role: str | None = None  # everyone whose cached platform roles hold it
    email: str | None = None  # a plain mailbox (fallback admin addresses)


@dataclass(frozen=True)
class PlannedMail:
    kind: str
    category: Category
    targets: tuple[Target, ...]
    params: dict[str, Any] = field(default_factory=dict)
    # the actor is normally skipped; job.complete is addressed to its actor
    to_actor: bool = False


def plan(
    record: AuditRecord,
    model: ModelRef | None,
    fallback_admin_emails: Sequence[str] = (),
) -> list[PlannedMail]:
    """The mails *record* causes. Empty for every action not in the catalogue."""
    details = record.details or {}
    action = record.action

    if action == "job.complete":
        if not record.actor_sub:  # anonymous job: nobody to tell
            return []
        return [
            PlannedMail(
                "job_complete",
                "jobs",
                (Target(sub=record.actor_sub),),
                {
                    "job_id": record.job_id,
                    "process_id": details.get("process_id"),
                    "status": details.get("status"),
                },
                to_actor=True,
            )
        ]

    if record.outcome != "success" or model is None:
        return []
    owner = Target(sub=model.owner_sub)
    verifiers = Target(role=roles.VERIFIER)

    if action == "model.status.change":
        frm, to = details.get("from"), details.get("to")
        if to == "submitted":
            return [PlannedMail("model_submitted", "review", (verifiers,))]
        if to == "draft":  # submission withdrawn before anyone picked it up
            return []
        return [
            PlannedMail(
                "model_status",
                "review",
                (owner,),
                {"from": frm, "to": to, "note": details.get("note")},
            )
        ]
    if action == "model.reverify":
        return [
            PlannedMail(
                "model_reverified", "review", (owner,), {"note": details.get("note")}
            )
        ]
    if action in ("model.interface.drifted", "model.interface.unreachable"):
        state = action.rsplit(".", 1)[1]
        return [
            PlannedMail(
                "model_interface",
                "drift",
                (owner, verifiers),
                {"state": state, "error": details.get("error")},
            )
        ]
    if action == "model.interface.restored":
        return [
            PlannedMail("model_interface", "drift", (owner,), {"state": "restored"})
        ]
    if action == "model.owner.transfer":
        before, after = details.get("before"), details.get("after")
        return [
            PlannedMail(
                "model_owner_transfer",
                "ownership",
                tuple(Target(sub=s) for s in (before, after) if s),
                {"previous_owner": before, "new_owner": after},
            )
        ]
    if action == "access.request":
        return [
            PlannedMail(
                "access_requested",
                "access",
                (
                    Target(role=roles.ACCESS_ADMIN),
                    *(Target(email=e) for e in fallback_admin_emails),
                    owner,
                ),
                {
                    "access_request_id": details.get("access_request_id"),
                    "user_sub": details.get("user_sub"),
                    "role": details.get("role"),
                    "note": details.get("note"),
                },
            )
        ]
    if action in ("access.grant", "access.deny", "access.revoke"):
        user = details.get("user_sub")
        if not user:
            return []
        return [
            PlannedMail(
                "access_decided",
                "access",
                (Target(sub=user),),
                {
                    "decision": {
                        "access.grant": "granted",
                        "access.deny": "denied",
                        "access.revoke": "revoked",
                    }[action],
                    "access_request_id": details.get("access_request_id"),
                    "role": details.get("role"),
                    "note": details.get("note"),
                },
            )
        ]
    return []
