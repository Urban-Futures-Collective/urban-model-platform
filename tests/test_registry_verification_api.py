"""Registry API phase C over HTTP: review workflow, verification, reverify,
owner transfer. In-memory registry and job store, in-test SafeFetchPort."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

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
from ump.core.models.job import Job
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.audit import AuditEvent
from ump.core.services.authorization import AuthorizationService

BASE = "https://srv.example.org/"
SEED = AuditEvent(actor_sub=None, action="test.seed")
CALLERS = {
    "owner": AuthContext("owner-1", ["provider"], True),
    "owner-verifier": AuthContext("owner-1", ["provider", "verifier"], True),
    "verifier": AuthContext("ver-1", ["verifier"], True),
    "verifier2": AuthContext("ver-2", ["verifier"], True),
    "admin": AuthContext("adm-1", ["platform-admin"], True),
    "viewer": AuthContext("view-1", ["viewer"], True),
}


class TokenAuth(AuthPort):
    async def verify(self, token):
        return CALLERS[token] if token else AuthContext(None, [], False)


class SpyProviders(ProvidersPort):
    def __init__(self):
        self.reloads = 0

    def load_providers(self):
        self.reloads += 1

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


class Descriptions(SafeFetchPort):
    def __init__(self):
        self.description: Dict[str, Any] = {
            "id": "echo",
            "title": "Echo",
            "inputs": {"x": {"schema": {"type": "number"}}},
            "outputs": {"y": {"schema": {"type": "number"}}},
        }

    def check_url(self, url):
        pass

    async def get_json(self, url, headers=None):
        assert url == BASE + "processes/echo"
        assert headers["Accept-Language"] == "en"  # hash = default-language answer
        return self.description


class Env:
    def __init__(self):
        self.repo = InMemoryRegistryRepository()
        self.jobs = InMemoryJobRepository("scratch/test_registry_verification")
        self.providers = SpyProviders()
        self.fetch = Descriptions()
        authz = AuthorizationService(self.providers)
        validator = ColonProcessId()

        class _StubJobManager:
            async def shutdown(self):
                return None

        self.app = create_app(
            process_manager_factory=lambda c: ProcessManager(
                self.providers, c, process_id_validator=validator
            ),
            http_client=NoHttp(),
            job_manager_factory=lambda c, pm: _StubJobManager(),
            job_repo=self.jobs,
            process_id_validator=validator,
            auth_port=TokenAuth(),
            authorization_service=authz,
            registry=RegistryServices.create(
                self.repo,
                authorization=authz,
                providers=self.providers,
                fetch=self.fetch,
                remote_auth=RemoteAuthAdapter(),
                cipher=None,
                languages=["en", "de"],
                public_catalogue=True,
                jobs=self.jobs,
                audit_log=InMemoryAuditLog(),
            ),
        )
        self.model = asyncio.run(self._seed())

    async def _seed(self) -> RegistryModel:
        await self.repo.upsert_user(AppUser(sub="owner-1"))
        server = await self.repo.create_provider_server(
            ProviderServer(name="srv", base_url=BASE, owner_sub="owner-1"), audit=SEED
        )
        (model,) = await self.repo.create_models(
            [
                RegistryModel(
                    slug="srv-echo",
                    provider_server_id=server.id,
                    remote_process_id="echo",
                    owner_sub="owner-1",
                    full_name={"en": "Echo"},
                    purpose={"en": "Test"},
                    license="MIT",
                )
            ],
            audit=[SEED],
        )
        return model

    def job(self, job_id, user="ver-1", process="srv:echo", status="successful", ago=0):
        created = datetime.now(timezone.utc) - timedelta(seconds=ago)
        asyncio.run(
            self.jobs.create(
                Job(
                    id=job_id,
                    user_id=user,
                    process_id=process,
                    provider="srv",
                    status=status,
                    created=created,
                )
            )
        )

    def stored(self) -> RegistryModel:
        return next(iter(self.repo.models.values()))


def _step(client, env, who, to, **body):
    return client.post(
        f"/registry/v1/models/{env.model.id}/transitions",
        headers={"Authorization": f"Bearer {who}"},
        json={"to_status": to, **body},
    )


def _to_review(client, env):
    assert _step(client, env, "owner", "submitted").status_code == 200
    assert _step(client, env, "verifier", "in_review").status_code == 200


def test_full_lifecycle_to_published():
    env = Env()
    with TestClient(env.app) as client:
        _to_review(client, env)
        env.job("job-ok")
        verified = _step(
            client, env, "verifier", "verified", verification_job_id="job-ok"
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["review"]["verified_by"] == "ver-1"
        assert verified.headers["ETag"]

        model = env.stored()
        assert model.verified_interface == env.fetch.description
        assert len(model.verified_interface_sha256) == 64
        assert model.interface_state == "in_sync" and model.exec_state == "in_review"

        reloads = env.providers.reloads
        published = _step(client, env, "verifier", "published")
        assert published.json()["status"] == "published"
        assert env.stored().published_at is not None
        assert env.providers.reloads == reloads + 1  # /v1.0 may run it now

        vocabulary = client.get(
            "/registry/v1/roles/vocabulary",
            headers={"Authorization": "Bearer verifier"},
        ).json()["roles"]
        assert vocabulary == ["srv", "srv:echo"]

        actions = [(e.action, e.details.get("to")) for e in env.repo.audit[2:]]
        assert actions == [
            ("model.status.change", "submitted"),
            ("model.status.change", "in_review"),
            ("model.status.change", "verified"),
            ("model.status.change", "published"),
        ]
        assert env.repo.audit[-1].actor_roles == ["verifier"]


@pytest.mark.parametrize(
    "job_kwargs, why",
    [
        (None, "no job"),
        ({"user": "ver-2"}, "not started by you"),
        ({"process": "srv:other"}, "did not run"),
        ({"status": "failed"}, "not 'successful'"),
        ({"ago": 3600}, "before the current review round"),
    ],
)
def test_verification_job_is_checked(job_kwargs, why):
    env = Env()
    with TestClient(env.app) as client:
        _to_review(client, env)
        if job_kwargs is not None:
            env.job("job-x", **job_kwargs)
        response = _step(
            client, env, "verifier", "verified", verification_job_id="job-x"
        )
        assert response.status_code == 400
        assert why in response.json()["detail"]
        assert env.stored().status == "in_review"


def test_owner_never_verifies_own_model_even_as_verifier():
    env = Env()
    with TestClient(env.app) as client:
        _to_review(client, env)
        env.job("job-own", user="owner-1")
        response = _step(
            client, env, "owner-verifier", "verified", verification_job_id="job-own"
        )
        assert response.status_code == 403


def test_reviewer_feedback_needs_a_note_and_round_trips():
    env = Env()
    with TestClient(env.app) as client:
        _to_review(client, env)
        assert _step(client, env, "verifier", "changes_requested").status_code == 400
        sent_back = _step(
            client, env, "verifier", "changes_requested", note="add units"
        )
        assert sent_back.json()["review"]["status_note"] == "add units"
        # the owner sees the reason and resubmits
        assert _step(client, env, "owner", "submitted").status_code == 200
        assert _step(client, env, "owner", "published").status_code == 409


def test_reverify_updates_interface_in_place():
    env = Env()
    with TestClient(env.app) as client:
        _to_review(client, env)
        env.job("job-1")
        _step(client, env, "verifier", "verified", verification_job_id="job-1")
        _step(client, env, "verifier", "published")
        first_sha = env.stored().verified_interface_sha256

        env.fetch.description = {**env.fetch.description, "outputs": {"z": {}}}
        env.job("job-2", user="ver-2")
        reverified = client.post(
            f"/registry/v1/models/{env.model.id}/reverify",
            headers={"Authorization": "Bearer verifier2"},
            json={"verification_job_id": "job-2"},
        )
        assert reverified.status_code == 200, reverified.text
        model = env.stored()
        assert model.status == "published" and model.verified_by == "ver-2"
        assert model.verified_interface_sha256 != first_sha
        assert env.repo.audit[-1].action == "model.reverify"

        # reusing a job from before the last verification is refused
        again = client.post(
            f"/registry/v1/models/{env.model.id}/reverify",
            headers={"Authorization": "Bearer verifier"},
            json={"verification_job_id": "job-1"},
        )
        assert again.status_code == 400


def test_reset_and_deactivation_change_what_ogc_may_run():
    env = Env()
    with TestClient(env.app) as client:
        _to_review(client, env)
        env.job("job-1")
        _step(client, env, "verifier", "verified", verification_job_id="job-1")
        _step(client, env, "verifier", "published")
        first_published = env.stored().published_at

        reloads = env.providers.reloads
        assert (
            _step(client, env, "verifier", "in_review", note="drifted").status_code
            == 200
        )
        assert env.stored().exec_state == "in_review"
        assert env.providers.reloads == reloads + 1

        env.job("job-2")
        _step(client, env, "verifier", "verified", verification_job_id="job-2")
        _step(client, env, "verifier", "published")
        assert env.stored().published_at == first_published  # first publication only

        assert _step(client, env, "owner", "deactivated", note="x").status_code == 403
        gone = _step(client, env, "admin", "deactivated", note="licence revoked")
        assert gone.status_code == 200 and env.stored().exec_state == "deactivated"
        assert _step(client, env, "verifier", "published").status_code == 409


def test_stale_if_match_on_transition():
    env = Env()
    with TestClient(env.app) as client:
        response = client.post(
            f"/registry/v1/models/{env.model.id}/transitions",
            headers={"Authorization": "Bearer owner", "If-Match": '"stale"'},
            json={"to_status": "submitted"},
        )
        assert response.status_code == 412


def test_invisible_model_is_404_for_transitions():
    env = Env()
    with TestClient(env.app) as client:
        # a draft is invisible to verifiers
        assert _step(client, env, "verifier", "in_review").status_code == 404
        assert _step(client, env, "viewer", "submitted").status_code == 404


def test_owner_transfer():
    env = Env()
    with TestClient(env.app) as client:
        url = f"/registry/v1/models/{env.model.id}/owner"
        etag = client.get(
            f"/registry/v1/models/{env.model.id}",
            headers={"Authorization": "Bearer owner"},
        ).headers["ETag"]
        body = {"owner_sub": "new-owner"}
        assert (
            client.patch(
                url,
                headers={"Authorization": "Bearer verifier", "If-Match": etag},
                json=body,
            ).status_code
            == 403
        )
        assert (
            client.patch(
                url, headers={"Authorization": "Bearer admin"}, json=body
            ).status_code
            == 428
        )
        moved = client.patch(
            url, headers={"Authorization": "Bearer admin", "If-Match": etag}, json=body
        )
        assert moved.status_code == 200 and moved.json()["owner_sub"] == "new-owner"
        assert "new-owner" in env.repo.users
        assert env.repo.audit[-1].details == {"before": "owner-1", "after": "new-owner"}


def test_owner_transfer_respects_four_eyes():
    env = Env()
    with TestClient(env.app) as client:
        _to_review(client, env)
        env.job("job-1")
        _step(client, env, "verifier", "verified", verification_job_id="job-1")
        etag = client.get(
            f"/registry/v1/models/{env.model.id}",
            headers={"Authorization": "Bearer admin"},
        ).headers["ETag"]
        response = client.patch(
            f"/registry/v1/models/{env.model.id}/owner",
            headers={"Authorization": "Bearer admin", "If-Match": etag},
            json={"owner_sub": "ver-1"},  # the verifier of this model
        )
        assert response.status_code == 409
