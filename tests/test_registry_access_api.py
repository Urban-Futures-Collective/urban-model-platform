"""Registry API phase E1 over HTTP: access requests, decisions, direct grants,
visibility. In-memory registry, in-test auth."""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ump.adapters.audit_log_inmemory import InMemoryAuditLog
from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
from ump.adapters.web.fastapi import create_app
from ump.adapters.web.registry_router import RegistryServices
from ump.core.interfaces.auth import AuthContext, AuthPort
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.safe_fetch import SafeFetchPort
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.audit import AuditEvent
from ump.core.services.authorization import AuthorizationService

SEED = AuditEvent(actor_sub=None, action="test.seed")
SHA = "a" * 64
CALLERS = {
    "user": AuthContext("user-1", ["user"], True, email="u1@example.org", name="U1"),
    "user2": AuthContext("user-2", ["user"], True),
    "holder": AuthContext("user-3", ["user", "srv:echo"], True),
    "provider-holder": AuthContext("user-4", ["user", "srv"], True),
    "owner": AuthContext("owner-1", ["provider"], True),
    "admin": AuthContext("adm-1", ["access-admin"], True),
    "admin-user": AuthContext("user-1", ["user", "access-admin"], True),
}


class TokenAuth(AuthPort):
    async def verify(self, token):
        return CALLERS[token] if token else AuthContext(None, [], False)


class NoProviders(ProvidersPort):
    def load_providers(self):
        pass

    def get_providers(self):
        return []

    def get_provider(self, name):
        return None

    def get_process_config(self, name, pid):
        return None

    def list_providers(self):
        return []

    def get_processes(self, name):
        return []

    def check_process_availability(self, name, pid):
        return False


class NoHttp(HttpClientPort):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, timeout=None, headers=None):
        raise AssertionError

    async def get_content(self, url, timeout=None, headers=None):
        raise AssertionError

    async def post(self, url, json=None, timeout=None, headers=None):
        raise AssertionError

    async def close(self):
        return None


class NoFetch(SafeFetchPort):
    def check_url(self, url):
        pass

    async def get_json(self, url, headers=None):
        raise AssertionError


class Env:
    def __init__(self):
        self.repo = InMemoryRegistryRepository()
        providers = NoProviders()
        authz = AuthorizationService(providers)
        validator = ColonProcessId()

        class _StubJobManager:
            async def shutdown(self):
                return None

        self.app = create_app(
            process_manager_factory=lambda c: ProcessManager(
                providers, c, process_id_validator=validator
            ),
            http_client=NoHttp(),
            job_manager_factory=lambda c, pm: _StubJobManager(),
            job_repo=InMemoryJobRepository("scratch/test_registry_access"),
            process_id_validator=validator,
            auth_port=TokenAuth(),
            authorization_service=authz,
            registry=RegistryServices.create(
                self.repo,
                authorization=authz,
                providers=providers,
                fetch=NoFetch(),
                remote_auth=RemoteAuthAdapter(),
                cipher=None,
                languages=["en", "de"],
                public_catalogue=True,
                jobs=InMemoryJobRepository("scratch/test_registry_access"),
                audit_log=InMemoryAuditLog(),
            ),
        )
        self.models = asyncio.run(self._seed())

    async def _seed(self) -> dict[str, RegistryModel]:
        await self.repo.upsert_user(AppUser(sub="owner-1"))
        server = await self.repo.create_provider_server(
            ProviderServer(name="srv", base_url="https://srv/", owner_sub="owner-1"),
            audit=SEED,
        )

        def model(pid, status="published", **extra):
            published = status == "published"
            return RegistryModel(
                slug=f"srv-{pid}",
                provider_server_id=server.id,
                remote_process_id=pid,
                owner_sub="owner-1",
                full_name={"en": f"{pid} model", "de": f"{pid} Modell"},
                purpose={"en": "Test"},
                license="MIT",
                status=status,
                published_at=datetime.now(timezone.utc) if published else None,
                verified_by="ver-1" if published else None,
                verified_interface_sha256=SHA if published else None,
                **extra,
            )

        created = await self.repo.create_models(
            [
                model("echo"),
                model("other"),
                model("anon", process_config={"anonymous-access": True}),
                model("draft", status="draft"),
            ],
            audit=[SEED],
        )
        return {m.remote_process_id: m for m in created}

    def set_model_status(self, pid, status):
        key = next(k for k in self.repo.models if k[1] == pid)
        self.repo.models[key] = self.repo.models[key].model_copy(
            update={"status": status}
        )

    def actions(self):
        return [e.action for e in self.repo.audit if e.action.startswith("access.")]


def _h(who):
    return {"Authorization": f"Bearer {who}"} if who else {}


def _request(client, env, who="user", pid="echo", **body):
    return client.post(
        f"/registry/v1/models/{env.models[pid].id}/access-requests",
        headers=_h(who),
        json=body,
    )


def _step(client, request_id, who, to, note=None, **headers):
    return client.post(
        f"/registry/v1/access-requests/{request_id}/transitions",
        headers={**_h(who), **headers},
        json={"to_status": to, "note": note},
    )


def test_request_is_recorded_audited_and_resolved():
    env = Env()
    with TestClient(env.app) as client:
        response = _request(client, env, note="for my thesis")
        assert response.status_code == 201, response.text
        body = response.json()
        assert (
            response.headers["Location"] == f"/registry/v1/access-requests/{body['id']}"
        )
        assert response.headers["ETag"]
        assert body["status"] == "requested"
        assert body["role"] == "srv:echo"
        assert body["model"]["process_id"] == "srv:echo"
        assert body["model"]["full_name"] == "echo model"
        assert body["user"] == {"sub": "user-1", "display_name": "U1", "email": None}
        assert body["request_note"] == "for my thesis"

        event = env.repo.audit[-1]
        assert event.action == "access.request"
        assert event.actor_sub == "user-1"
        assert event.model_id == env.models["echo"].id
        assert event.details == {
            "access_request_id": body["id"],
            "user_sub": "user-1",
            "role": "srv:echo",
            "note": "for my thesis",
        }

        german = client.get(
            f"/registry/v1/access-requests/{body['id']}?lang=de", headers=_h("user")
        )
        assert german.json()["model"]["full_name"] == "echo Modell"


@pytest.mark.parametrize(
    "who, pid, status",
    [
        (None, "echo", 401),
        ("user", "draft", 404),  # not visible to the user
        ("user", "anon", 409),  # runs without access rights
        ("holder", "echo", 409),  # token already holds srv:echo
        ("provider-holder", "echo", 409),  # token already holds srv
    ],
)
def test_pointless_or_invisible_requests_are_refused(who, pid, status):
    env = Env()
    with TestClient(env.app) as client:
        assert _request(client, env, who=who, pid=pid).status_code == status
        assert env.actions() == []


def test_request_for_a_model_that_is_not_published_is_refused():
    env = Env()
    env.set_model_status("echo", "deactivated")
    with TestClient(env.app) as client:
        # an owner sees their deactivated model, but cannot request it either
        response = client.post(
            f"/registry/v1/models/{env.models['echo'].id}/access-requests",
            headers=_h("owner"),
            json={},
        )
        assert response.status_code == 409


def test_one_open_request_per_user_and_model():
    env = Env()
    with TestClient(env.app) as client:
        first = _request(client, env).json()
        assert _request(client, env).status_code == 409
        assert _request(client, env, pid="other").status_code == 201
        assert (
            _step(client, first["id"], "admin", "denied", "not now").status_code == 200
        )
        assert _request(client, env).status_code == 201  # a new lifecycle


def test_grant_deny_revoke_withdraw():
    env = Env()
    with TestClient(env.app) as client:
        rid = _request(client, env).json()["id"]
        granted = _step(client, rid, "admin", "granted")
        assert granted.status_code == 200, granted.text
        assert granted.json()["status"] == "granted"
        assert granted.json()["status_changed_by"] == "adm-1"
        assert _step(client, rid, "admin", "revoked").status_code == 400  # note
        revoked = _step(client, rid, "admin", "revoked", "left the project")
        assert revoked.json()["status"] == "revoked"
        assert revoked.json()["status_note"] == "left the project"

        rid2 = _request(client, env).json()["id"]
        assert _step(client, rid2, "admin", "denied").status_code == 400  # note
        assert _step(client, rid2, "admin", "withdrawn").status_code == 403
        assert _step(client, rid2, "user", "withdrawn").status_code == 200
        assert _step(client, rid2, "admin", "granted").status_code == 409

        assert env.actions() == [
            "access.request",
            "access.grant",
            "access.revoke",
            "access.request",
            "access.withdraw",
        ]
        revoke = [e for e in env.repo.audit if e.action == "access.revoke"][0]
        assert revoke.details["from"] == "granted" and revoke.details["to"] == "revoked"


def test_requester_cannot_decide_own_request():
    env = Env()
    with TestClient(env.app) as client:
        rid = _request(client, env, who="admin-user").json()["id"]
        assert _step(client, rid, "admin-user", "granted").status_code == 403
        assert _step(client, rid, "admin-user", "denied", "no").status_code == 403


def test_only_access_admins_decide_and_others_do_not_see_the_request():
    env = Env()
    with TestClient(env.app) as client:
        rid = _request(client, env).json()["id"]
        assert _step(client, rid, "user", "granted").status_code == 403
        assert _step(client, rid, "owner", "granted").status_code == 403
        assert _step(client, rid, "user2", "granted").status_code == 404
        assert (
            client.get(
                f"/registry/v1/access-requests/{rid}", headers=_h("user2")
            ).status_code
            == 404
        )


def test_grant_needs_a_published_model():
    env = Env()
    with TestClient(env.app) as client:
        rid = _request(client, env).json()["id"]
        env.set_model_status("echo", "in_review")  # reset after the request
        response = _step(client, rid, "admin", "granted")
        assert response.status_code == 409
        assert "Deny the request" in response.json()["detail"]
        assert (
            _step(client, rid, "admin", "denied", "model in review").status_code == 200
        )


def test_if_match_is_optional_but_checked():
    env = Env()
    with TestClient(env.app) as client:
        created = _request(client, env)
        rid, etag = created.json()["id"], created.headers["ETag"]
        assert (
            _step(
                client, rid, "admin", "granted", **{"If-Match": '"stale"'}
            ).status_code
            == 412
        )
        ok = _step(client, rid, "admin", "granted", **{"If-Match": etag})
        assert ok.status_code == 200


def test_list_visibility_and_email():
    env = Env()
    with TestClient(env.app) as client:
        _request(client, env, who="user")
        _request(client, env, who="user2", pid="other")

        def listed(who, query=""):
            response = client.get(
                f"/registry/v1/access-requests{query}", headers=_h(who)
            )
            assert response.status_code == 200, response.text
            return response.json()["requests"]

        assert [r["user"]["sub"] for r in listed("user")] == ["user-1"]
        assert [r["user"]["sub"] for r in listed("user2")] == ["user-2"]
        assert {r["user"]["sub"] for r in listed("owner")} == {"user-1", "user-2"}
        admin_view = listed("admin")
        assert {r["user"]["sub"] for r in admin_view} == {"user-1", "user-2"}
        assert {r["user"]["email"] for r in admin_view} == {"u1@example.org", None}
        assert all(r["user"]["email"] is None for r in listed("owner"))

        # filters narrow, never widen, what a caller sees
        assert listed("user", "?user=user-2") == []
        assert [r["user"]["sub"] for r in listed("admin", "?user=user-2")] == ["user-2"]
        assert listed("admin", "?status=granted") == []
        model_id = env.models["other"].id
        assert len(listed("admin", f"?model_id={model_id}")) == 1

        assert client.get("/registry/v1/access-requests").status_code == 401
        assert (
            client.get(
                "/registry/v1/access-requests?status=bogus", headers=_h("admin")
            ).status_code
            == 400
        )


def test_list_is_newest_first_and_paginated():
    env = Env()
    with TestClient(env.app) as client:
        ids = [_request(client, env, pid=pid).json()["id"] for pid in ("echo", "other")]
        ids.append(_request(client, env, who="user2").json()["id"])
        first = client.get(
            "/registry/v1/access-requests?limit=2", headers=_h("admin")
        ).json()
        assert [r["id"] for r in first["requests"]] == ids[::-1][:2]
        second = client.get(
            f"/registry/v1/access-requests?limit=2&cursor={first['next_cursor']}",
            headers=_h("admin"),
        ).json()
        assert [r["id"] for r in second["requests"]] == [ids[0]]
        assert second["next_cursor"] is None
        assert (
            client.get(
                "/registry/v1/access-requests?cursor=!!", headers=_h("admin")
            ).status_code
            == 400
        )


def _grant(client, env, who="admin", pid="echo", **user):
    return client.post(
        f"/registry/v1/models/{env.models[pid].id}/access-grants",
        headers=_h(who),
        json={"user": user, "note": "from the matrix"},
    )


def test_direct_grant_records_and_creates_unknown_user_once():
    env = Env()
    with TestClient(env.app) as client:
        response = _grant(client, env, sub="kc-9", email="new@example.org", name="New")
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "granted"
        assert response.json()["request_note"] is None
        assert response.json()["user"]["email"] == "new@example.org"
        event = env.repo.audit[-1]
        assert event.action == "access.grant" and event.details["direct"] is True

        # an account UMP knows is never overwritten from the admin's data
        _request(client, env, pid="other")  # user-1 recorded from the token
        _grant(client, env, pid="anon", sub="user-1", email="wrong@example.org")
        assert env.repo.users["user-1"].email == "u1@example.org"


def test_direct_grant_rules():
    env = Env()
    with TestClient(env.app) as client:
        assert _grant(client, env, who="user", sub="kc-9").status_code == 403
        assert _grant(client, env, sub="adm-1").status_code == 403  # self
        assert _grant(client, env, pid="draft", sub="kc-9").status_code == 409
        _request(client, env)  # user-1 has an open request
        conflict = _grant(client, env, sub="user-1")
        assert conflict.status_code == 409
        assert "decide the pending request" in conflict.json()["detail"]
