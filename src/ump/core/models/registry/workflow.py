"""Review workflow (REF-F12 "Review workflow") — pure domain logic.

    draft → submitted → in_review → verified → published
                ↑           │ │                    │
                └── changes_requested   rejected    └→ in_review (reset)
    any of in_review / verified / published → deactivated

Who may take a step:

- ``owner``: the model's owner, holding ``provider``;
- ``reviewer``: a ``verifier`` who is **not** the owner (four-eyes, D6/CHECK);
- ``deactivator``: a ``verifier`` or a ``platform-admin``.

The database keeps only the invariants (0003 CHECKs); the graph and the role
rules live here, because roles are only visible in the token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ump.core.interfaces.auth import AuthContext
from ump.core.models.registry import RegistryModel, roles

Actor = Literal["owner", "reviewer", "deactivator"]


@dataclass(frozen=True)
class Step:
    actor: Actor
    needs_note: bool = False  # the reason is shown to the other side
    needs_verification_job: bool = False


TRANSITIONS: dict[tuple[str, str], Step] = {
    ("draft", "submitted"): Step("owner"),
    ("submitted", "draft"): Step("owner"),  # withdraw a submission
    ("changes_requested", "submitted"): Step("owner"),
    ("submitted", "in_review"): Step("reviewer"),  # pick up
    ("in_review", "verified"): Step("reviewer", needs_verification_job=True),
    ("in_review", "changes_requested"): Step("reviewer", needs_note=True),
    ("in_review", "rejected"): Step("reviewer", needs_note=True),
    ("verified", "published"): Step("reviewer"),
    ("published", "in_review"): Step("reviewer", needs_note=True),  # reset
    ("in_review", "deactivated"): Step("deactivator", needs_note=True),
    ("verified", "deactivated"): Step("deactivator", needs_note=True),
    ("published", "deactivated"): Step("deactivator", needs_note=True),
}

# Statuses from which the card must be complete in the default language
# before moving on (stricter than the CHECK, which starts at in_review, so
# the owner learns it when submitting rather than when a verifier picks up).
NEEDS_COMPLETE_CARD = frozenset({"submitted", "in_review", "verified", "published"})


class TransitionError(Exception):
    """The step cannot be taken: not in the graph or the card is incomplete
    (status 409), or the request lacks something the step needs (400)."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class TransitionForbidden(Exception):
    """The caller may not take this step (403)."""


def may_act(actor: Actor, model: RegistryModel, auth: AuthContext) -> bool:
    if not auth.is_authenticated:
        return False
    held = set(auth.roles)
    is_owner = model.owner_sub == auth.user_id
    if actor == "owner":
        return is_owner and roles.PROVIDER in held
    if actor == "reviewer":
        return roles.VERIFIER in held and not is_owner
    return bool({roles.VERIFIER, roles.PLATFORM_ADMIN} & held)


def check_transition(
    model: RegistryModel,
    to_status: str,
    auth: AuthContext,
    note: str | None,
    verification_job_id: str | None,
) -> Step:
    """The step for ``model.status → to_status`` if *auth* may take it now."""
    step = TRANSITIONS.get((model.status, to_status))
    if step is None:
        allowed = sorted(to for (frm, to) in TRANSITIONS if frm == model.status)
        raise TransitionError(
            f"'{model.status}' cannot move to '{to_status}'"
            + (f"; allowed: {', '.join(allowed)}" if allowed else "")
        )
    if not may_act(step.actor, model, auth):
        who = {
            "owner": "the owner (role provider)",
            "reviewer": "a verifier who is not the owner",
            "deactivator": "a verifier or platform admin",
        }[step.actor]
        raise TransitionForbidden(f"'{model.status}' → '{to_status}' is for {who}.")
    if step.needs_note and not (note and note.strip()):
        raise TransitionError(f"'{to_status}' needs a note explaining why.", 400)
    if step.needs_verification_job and not verification_job_id:
        raise TransitionError(
            "'verified' needs the verification_job_id of your run.", 400
        )
    if not step.needs_verification_job and verification_job_id:
        raise TransitionError(
            "verification_job_id is only accepted for 'verified'.", 400
        )
    if to_status in NEEDS_COMPLETE_CARD:
        missing = [
            what
            for what, ok in (
                (
                    f"purpose in '{model.default_lang}'",
                    model.default_lang in model.purpose,
                ),
                ("license", bool(model.license)),
            )
            if not ok
        ]
        if missing:
            raise TransitionError("The model card is incomplete: " + ", ".join(missing))
    return step
