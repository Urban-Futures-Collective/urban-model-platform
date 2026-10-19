"""Review workflow graph and role rules (pure core, REF-F12 "Review workflow")."""

from itertools import product
from uuid import uuid4

import pytest

from ump.core.interfaces.auth import AuthContext
from ump.core.models.registry import RegistryModel
from ump.core.models.registry.workflow import (
    TRANSITIONS,
    TransitionError,
    TransitionForbidden,
    check_transition,
)

STATUSES = [
    "draft",
    "submitted",
    "in_review",
    "changes_requested",
    "verified",
    "published",
    "rejected",
    "deactivated",
]

OWNER = AuthContext("owner", ["provider"], True)
OWNER_VERIFIER = AuthContext("owner", ["provider", "verifier"], True)
VERIFIER = AuthContext("ver", ["verifier"], True)
ADMIN = AuthContext("adm", ["platform-admin"], True)
STRANGER = AuthContext("x", ["provider"], True)
ANON = AuthContext(None, [], False)

# the caller that may take each step, plus what the step needs
RIGHT_CALLER = {"owner": OWNER, "reviewer": VERIFIER, "deactivator": ADMIN}


def _model(status: str, complete: bool = True) -> RegistryModel:
    return RegistryModel(
        slug="m",
        provider_server_id=uuid4(),
        remote_process_id="p",
        owner_sub="owner",
        full_name={"en": "M"},
        purpose={"en": "Purpose"} if complete else {},
        license="MIT" if complete else None,
        status=status,
    )


def _args(step):
    return {
        "note": "because" if step.needs_note else None,
        "verification_job_id": "job-1" if step.needs_verification_job else None,
    }


@pytest.mark.parametrize("frm, to", list(product(STATUSES, STATUSES)))
def test_graph_is_exactly_the_specified_one(frm, to):
    step = TRANSITIONS.get((frm, to))
    if step is None:
        for caller in (OWNER, VERIFIER, ADMIN):
            with pytest.raises(TransitionError) as excinfo:
                check_transition(_model(frm), to, caller, "n", None)
            assert excinfo.value.status == 409
    else:
        caller = RIGHT_CALLER[step.actor]
        assert check_transition(_model(frm), to, caller, **_args(step)) is step


def test_graph_matches_the_spec():
    assert set(TRANSITIONS) == {
        ("draft", "submitted"),
        ("submitted", "draft"),
        ("changes_requested", "submitted"),
        ("submitted", "in_review"),
        ("in_review", "verified"),
        ("in_review", "changes_requested"),
        ("in_review", "rejected"),
        ("verified", "published"),
        ("published", "in_review"),
        ("in_review", "deactivated"),
        ("verified", "deactivated"),
        ("published", "deactivated"),
    }


@pytest.mark.parametrize("frm, to", sorted(TRANSITIONS))
def test_wrong_callers_are_forbidden(frm, to):
    step = TRANSITIONS[(frm, to)]
    wrong = {
        "owner": [STRANGER, VERIFIER, ADMIN, ANON],
        # four-eyes: the owner may not review, even holding verifier
        "reviewer": [OWNER, OWNER_VERIFIER, ADMIN, ANON],
        "deactivator": [OWNER, STRANGER, ANON],
    }[step.actor]
    for caller in wrong:
        with pytest.raises(TransitionForbidden):
            check_transition(_model(frm), to, caller, **_args(step))


def test_deactivation_by_verifier_or_admin():
    for caller in (VERIFIER, ADMIN, OWNER_VERIFIER):
        check_transition(_model("published"), "deactivated", caller, "broken", None)


@pytest.mark.parametrize("frm, to", [k for k, s in TRANSITIONS.items() if s.needs_note])
def test_notes_are_required_where_the_other_side_needs_a_reason(frm, to):
    caller = RIGHT_CALLER[TRANSITIONS[(frm, to)].actor]
    for note in (None, "", "   "):
        with pytest.raises(TransitionError) as excinfo:
            check_transition(_model(frm), to, caller, note, None)
        assert excinfo.value.status == 400


def test_verification_job_required_only_for_verified():
    with pytest.raises(TransitionError):
        check_transition(_model("in_review"), "verified", VERIFIER, None, None)
    with pytest.raises(TransitionError):
        check_transition(_model("verified"), "published", VERIFIER, None, "job-1")


@pytest.mark.parametrize(
    "frm, to, caller",
    [
        ("draft", "submitted", OWNER),
        ("changes_requested", "submitted", OWNER),
        ("submitted", "in_review", VERIFIER),
    ],
)
def test_incomplete_card_cannot_enter_review(frm, to, caller):
    with pytest.raises(TransitionError) as excinfo:
        check_transition(_model(frm, complete=False), to, caller, None, None)
    assert "purpose" in str(excinfo.value) and "license" in str(excinfo.value)


def test_withdrawing_needs_no_complete_card():
    check_transition(_model("submitted", complete=False), "draft", OWNER, None, None)
