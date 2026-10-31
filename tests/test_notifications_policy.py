"""Notification policy (REF-F12 "Notifications") — pure catalogue tests."""

import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from ump.core.models.registry.audit import AuditRecord
from ump.core.models.registry.notifications import (
    DEFAULT_PREFERENCES,
    KINDS,
    ModelRef,
    Target,
    effective_preferences,
    plan,
)

MODEL = ModelRef(
    id=uuid4(),
    slug="srv-echo",
    process_id="srv:echo",
    owner_sub="owner",
    full_name={"en": "Echo"},
    default_lang="en",
)
OWNER = Target(sub="owner")
VERIFIERS = Target(role="verifier")


def _record(action, actor="actor", outcome="success", **details):
    return AuditRecord(
        id=1,
        occurred_at=datetime.now(timezone.utc),
        actor_sub=actor,
        action=action,
        model_id=MODEL.id,
        job_id="job-1" if action.startswith("job.") else None,
        outcome=outcome,
        details=details,
    )


CATALOGUE = [
    # (action, details, kind, category, targets)
    ("model.status.change", {"from": "draft", "to": "submitted"},
     "model_submitted", "review", (VERIFIERS,)),
    ("model.status.change", {"from": "submitted", "to": "in_review"},
     "model_status", "review", (OWNER,)),
    ("model.status.change", {"from": "published", "to": "in_review", "note": "n"},
     "model_status", "review", (OWNER,)),
    *(
        ("model.status.change", {"from": "in_review", "to": to, "note": "n"},
         "model_status", "review", (OWNER,))
        for to in ("changes_requested", "rejected", "verified", "deactivated")
    ),
    ("model.status.change", {"from": "verified", "to": "published"},
     "model_status", "review", (OWNER,)),
    ("model.reverify", {}, "model_reverified", "review", (OWNER,)),
    ("model.interface.drifted", {}, "model_interface", "drift", (OWNER, VERIFIERS)),
    ("model.interface.unreachable", {"error": "timeout"},
     "model_interface", "drift", (OWNER, VERIFIERS)),
    ("model.interface.restored", {}, "model_interface", "drift", (OWNER,)),
    ("model.owner.transfer", {"before": "owner", "after": "new"},
     "model_owner_transfer", "ownership", (OWNER, Target(sub="new"))),
    ("access.request", {"user_sub": "u", "role": "srv:echo"},
     "access_requested", "access",
     (Target(role="access-admin"), Target(email="box@example.org"), OWNER)),
    *(
        (action, {"user_sub": "u"}, "access_decided", "access", (Target(sub="u"),))
        for action in ("access.grant", "access.deny", "access.revoke")
    ),
]  # fmt: skip


@pytest.mark.parametrize("action,details,kind,category,targets", CATALOGUE)
def test_catalogue(action, details, kind, category, targets):
    (mail,) = plan(_record(action, **details), MODEL, ["box@example.org"])
    assert (mail.kind, mail.category, mail.targets) == (kind, category, targets)
    assert not mail.to_actor


@pytest.mark.parametrize(
    "action,details",
    [
        ("model.status.change", {"from": "submitted", "to": "draft"}),  # withdrawn
        ("access.withdraw", {"user_sub": "u"}),
        ("model.card.update", {}),
        ("model.create", {}),
        ("server.update", {}),
        ("server.rediscover", {}),
        ("model.interface.changed", {}),
        ("job.execute", {}),
        ("something.new", {}),
    ],
)
def test_not_notified(action, details):
    assert plan(_record(action, **details), MODEL) == []


def test_unsuccessful_events_never_notify():
    record = _record("access.grant", outcome="denied", user_sub="u")
    assert plan(record, MODEL) == []


def test_model_events_need_the_model():
    assert plan(_record("model.reverify"), None) == []


def test_access_decision_names_the_decision_and_keeps_the_note():
    (mail,) = plan(_record("access.revoke", user_sub="u", note="left"), MODEL)
    assert mail.params["decision"] == "revoked" and mail.params["note"] == "left"


@pytest.mark.parametrize(
    "outcome,status", [("success", "successful"), ("failure", "failed")]
)
def test_job_complete_goes_to_its_actor_even_when_failed(outcome, status):
    record = _record("job.complete", actor="runner", outcome=outcome, status=status)
    (mail,) = plan(record, None)
    assert mail.kind == "job_complete" and mail.category == "jobs"
    assert mail.targets == (Target(sub="runner"),) and mail.to_actor
    assert mail.params["status"] == status


def test_anonymous_job_notifies_nobody():
    assert plan(_record("job.complete", actor=None, status="successful"), None) == []


def test_every_planned_kind_is_known():
    kinds = {
        mail.kind
        for action, details, *_ in CATALOGUE
        for mail in plan(_record(action, **details), MODEL)
    }
    kinds |= {plan(_record("job.complete", status="x"), None)[0].kind}
    assert kinds == KINDS


def test_catalogue_actions_exist_in_the_code():
    """Mails are keyed to audit action names; renaming one must break this."""
    source = "\n".join(
        p.read_text()
        for p in (Path(__file__).parents[1] / "src" / "ump").rglob("*.py")
        if p.name != "notifications.py" or "models" not in p.parts
    )
    emitted = set(re.findall(r'"((?:model|access|job)\.[a-z_.]+)"', source))
    actions = {a for a, *_ in CATALOGUE} | {"job.complete"}
    assert actions <= emitted, actions - emitted


def test_preferences_default_and_override():
    assert effective_preferences({}) == DEFAULT_PREFERENCES
    assert DEFAULT_PREFERENCES["jobs"] is False
    prefs = effective_preferences({"jobs": True, "drift": False, "bogus": True})
    assert prefs["jobs"] is True and prefs["drift"] is False and "bogus" not in prefs
