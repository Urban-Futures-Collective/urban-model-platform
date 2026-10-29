"""Access-request status graph and actor rules (REF-F12 phase E1) — pure core."""

from itertools import product
from uuid import uuid4

import pytest

from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.model_registry import AccessQuery
from ump.core.models.registry.access import (
    ACCESS_STATUSES,
    TRANSITIONS,
    AccessRequest,
    AccessTransitionError,
    AccessTransitionForbidden,
    check_transition,
    holds_access,
)

REQUESTER = AuthContext("user-1", ["user"], True)
ADMIN = AuthContext("adm-1", ["access-admin"], True)
ADMIN_REQUESTER = AuthContext("user-1", ["user", "access-admin"], True)
OTHER = AuthContext("user-2", ["user"], True)
ANONYMOUS = AuthContext(None, [], False)

EXPECTED = {
    ("requested", "withdrawn"): "access.withdraw",
    ("requested", "granted"): "access.grant",
    ("requested", "denied"): "access.deny",
    ("granted", "revoked"): "access.revoke",
}


def _request(status="requested"):
    return AccessRequest(user_sub="user-1", model_id=uuid4(), status=status)


def _actor_for(frm, to):
    return REQUESTER if (frm, to) == ("requested", "withdrawn") else ADMIN


def test_graph_is_exactly_the_documented_one():
    assert {k: v.action for k, v in TRANSITIONS.items()} == EXPECTED


@pytest.mark.parametrize("frm,to", sorted(product(ACCESS_STATUSES, repeat=2)))
def test_every_pair_outside_the_graph_is_rejected(frm, to):
    if (frm, to) in EXPECTED:
        step = check_transition(_request(frm), to, _actor_for(frm, to), "why")
        assert step.action == EXPECTED[(frm, to)]
    else:
        with pytest.raises(AccessTransitionError) as exc:
            check_transition(_request(frm), to, ADMIN, "why")
        assert exc.value.status == 409


@pytest.mark.parametrize("to", ["granted", "denied"])
def test_requester_cannot_decide_own_request_even_as_access_admin(to):
    with pytest.raises(AccessTransitionForbidden):
        check_transition(_request(), to, ADMIN_REQUESTER, "why")


@pytest.mark.parametrize("caller", [REQUESTER, OTHER, ANONYMOUS])
@pytest.mark.parametrize("to", ["granted", "denied"])
def test_only_access_admins_decide(caller, to):
    with pytest.raises(AccessTransitionForbidden):
        check_transition(_request(), to, caller, "why")


@pytest.mark.parametrize("caller", [REQUESTER, OTHER])
def test_only_access_admins_revoke(caller):
    with pytest.raises(AccessTransitionForbidden):
        check_transition(_request("granted"), "revoked", caller, "why")


@pytest.mark.parametrize("caller", [ADMIN, OTHER])
def test_only_the_requester_withdraws(caller):
    with pytest.raises(AccessTransitionForbidden):
        check_transition(_request(), "withdrawn", caller, None)


@pytest.mark.parametrize("frm,to", [("requested", "denied"), ("granted", "revoked")])
@pytest.mark.parametrize("note", [None, "", "   "])
def test_deny_and_revoke_need_a_note(frm, to, note):
    with pytest.raises(AccessTransitionError) as exc:
        check_transition(_request(frm), to, ADMIN, note)
    assert exc.value.status == 400


def test_grant_and_withdraw_need_no_note():
    check_transition(_request(), "granted", ADMIN, None)
    check_transition(_request(), "withdrawn", REQUESTER, None)


def test_holds_access_by_provider_or_process_role():
    assert holds_access(AuthContext("u", ["srv"], True), "srv", "srv:echo")
    assert holds_access(AuthContext("u", ["srv:echo"], True), "srv", "srv:echo")
    assert not holds_access(AuthContext("u", ["srv:other"], True), "srv", "srv:echo")
    assert not holds_access(AuthContext(None, ["srv"], False), "srv", "srv:echo")


def test_access_query_visibility_reference_semantics():
    record = _request()
    assert AccessQuery().matches(record, "owner-1")
    assert AccessQuery(visible_to="user-1").matches(record, "owner-1")
    assert AccessQuery(visible_to="owner-1").matches(record, "owner-1")
    assert not AccessQuery(visible_to="user-2").matches(record, "owner-1")
    assert not AccessQuery(status="granted").matches(record, None)
    assert not AccessQuery(user_sub="user-2").matches(record, None)
