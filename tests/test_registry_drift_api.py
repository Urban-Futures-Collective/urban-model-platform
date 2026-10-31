"""Registry API phase D over HTTP: GET /models/{id}/interface,
POST /servers/{id}/rediscover, GET /audit; plus the AuditObserver.
In-memory registry and audit log, in-test SafeFetchPort."""

import asyncio
import copy

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
from ump.core.interfaces.safe_fetch import RemoteFetchError, SafeFetchPort
from ump.core.managers.observers import AuditObserver
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.registry import (
    AppUser,
    ProviderServer,
    RegistryModel,
    interface_sha256,
)
from ump.core.models.registry.audit import AuditEvent
from ump.core.services.authorization import AuthorizationService

BASE = "https://srv.example.org/"
SEED = AuditEvent(actor_sub="owner-1", actor_roles=["provider"], action="test.seed")
DESCRIPTION = {
    "id": "echo",
    "inputs": {"x": {"schema": {"type": "number"}}},
    "outputs": {"y": {"schema": {"type": "number"}}},
}
CALLERS = {
    "owner": AuthContext("owner-1", ["provider"], True),
    "provider2": AuthContext("owner-2", ["provider"], True),
    "verifier": AuthContext("ver-1", ["verifier"], True),
    "access": AuthContext("acc-1", ["access-admin"], True),
    "admin": AuthContext("adm-1", ["platform-admin"], True),
    "viewer": AuthContext("view-1", ["viewer"], True),
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


class Server(SafeFetchPort):
    def __init__(self):
        self.description = copy.deepcopy(DESCRIPTION)
        self.down = False

    def check_url(self, url):
        pass

    async def get_json(self, url, headers=None):
        assert url == BASE + "processes/echo"
        if self.down:
            raise RemoteFetchError("connection refused")
        return copy.deepcopy(self.description)


class Env:
    def __init__(self):
        self.audit_log = InMemoryAuditLog()
        self.repo = InMemoryRegistryRepository(audit_log=self.audit_log)
        self.server = Server()
        providers = NoProviders()
        authz = AuthorizationService(providers)
        validator = ColonProcessId()
        jobs = InMemoryJobRepository("scratch/test_registry_drift_jobs")

        class _StubJobManager:
            async def shutdown(self):
                return None

        self.app = create_app(
            process_manager_factory=lambda c: ProcessManager(
                providers, c, process_id_validator=validator
            ),
            http_client=NoHttp(),
            job_manager_factory=lambda c, pm: _StubJobManager(),
            job_repo=jobs,
            process_id_validator=validator,
            auth_port=TokenAuth(),
            authorization_service=authz,
            registry=RegistryServices.create(
                self.repo,
                authorization=authz,
                providers=providers,
                fetch=self.server,
                remote_auth=RemoteAuthAdapter(),
                cipher=None,
                languages=["en", "de"],
                public_catalogue=True,
                jobs=jobs,
                audit_log=self.audit_log,
            ),
        )
        self.server_id, self.model = asyncio.run(self._seed())

    async def _seed(self):
        await self.repo.upsert_user(AppUser(sub="owner-1"))
        server = await self.repo.create_provider_server(
            ProviderServer(name="srv", base_url=BASE, owner_sub="owner-1"), audit=SEED
        )
        sha = interface_sha256(DESCRIPTION)
        (model,) = await self.repo.create_models(
            [
                RegistryModel(
                    slug="srv-echo",
                    provider_server_id=server.id,
                    remote_process_id="echo",
                    owner_sub="owner-1",
                    full_name={"en": "Echo"},
                    status="published",
                    verified_interface=DESCRIPTION,
                    verified_interface_sha256=sha,
                    current_interface_sha256=sha,
                    interface_state="in_sync",
                )
            ],
            audit=[SEED],
        )
        return server.id, model


def _as(who):
    return {"Authorization": f"Bearer {who}"} if who else {}


# --- GET /models/{id}/interface ----------------------------------------------


def test_interface_shows_live_diff_without_storing_it():
    env = Env()
    env.server.description["inputs"]["z"] = {"schema": {"type": "string"}}
    url = f"/registry/v1/models/{env.model.id}/interface"
    with TestClient(env.app) as client:
        for who in ("owner", "verifier", "admin"):
            r = client.get(url, headers=_as(who))
            assert r.status_code == 200, (who, r.text)
        body = r.json()
    assert body["interface_state"] == "in_sync"  # stored state: no check ran
    assert body["matches_verified"] is False
    assert body["live"]["sha256"] == interface_sha256(env.server.description)
    assert body["diff"] == {"inputs": {"added": ["z"], "removed": [], "changed": []}}
    assert body["verified_interface"] == DESCRIPTION
    stored = asyncio.run(env.repo.get_model_by_id(env.model.id))
    assert stored.interface_state == "in_sync" and stored.last_checked_at is None


def test_interface_reports_unreachable_server_in_the_body():
    env = Env()
    env.server.down = True
    with TestClient(env.app) as client:
        r = client.get(
            f"/registry/v1/models/{env.model.id}/interface", headers=_as("owner")
        )
    assert r.status_code == 200
    assert r.json()["live"] is None and "connection refused" in r.json()["live_error"]


def test_interface_authorization():
    env = Env()
    url = f"/registry/v1/models/{env.model.id}/interface"
    with TestClient(env.app) as client:
        assert client.get(url).status_code == 401
        assert client.get(url, headers=_as("viewer")).status_code == 403
        assert client.get(url, headers=_as("provider2")).status_code == 403


# --- POST /servers/{id}/rediscover -------------------------------------------


def test_rediscover_checks_now_and_audits_the_request():
    env = Env()
    env.server.description["outputs"] = {}
    url = f"/registry/v1/servers/{env.server_id}/rediscover"
    with TestClient(env.app) as client:
        r = client.post(url, headers=_as("owner"))
    assert r.status_code == 200, r.text
    (checked,) = r.json()["checked"]
    assert checked["previous_state"] == "in_sync"
    assert checked["interface_state"] == "drifted"
    actions = [(e.action, e.actor_sub) for e in env.repo.audit]
    assert ("server.rediscover", "owner-1") in actions
    assert ("model.interface.drifted", "owner-1") in actions


def test_rediscover_authorization():
    env = Env()
    url = f"/registry/v1/servers/{env.server_id}/rediscover"
    with TestClient(env.app) as client:
        assert client.post(url).status_code == 401
        assert client.post(url, headers=_as("verifier")).status_code == 403
        assert client.post(url, headers=_as("provider2")).status_code == 404
        assert client.post(url, headers=_as("admin")).status_code == 200


# --- GET /audit --------------------------------------------------------------


def test_audit_readers_and_filters():
    env = Env()
    with TestClient(env.app) as client:
        assert client.get("/registry/v1/audit").status_code == 401
        for who in ("owner", "viewer"):
            assert client.get("/registry/v1/audit", headers=_as(who)).status_code == 403
        for who in ("verifier", "access", "admin"):
            r = client.get("/registry/v1/audit", headers=_as(who))
            assert r.status_code == 200, (who, r.text)
        events = r.json()["events"]
        assert [e["action"] for e in events] == ["test.seed", "test.seed"]
        assert events[0]["id"] > events[1]["id"]  # newest first

        r = client.get(
            "/registry/v1/audit",
            params={"action": "nothing.like.this"},
            headers=_as("admin"),
        )
        assert r.json() == {"events": [], "next_cursor": None}


def test_audit_pagination():
    env = Env()
    for i in range(5):
        asyncio.run(env.audit_log.append(AuditEvent(actor_sub="x", action=f"a{i}")))
    seen = []
    cursor = None
    with TestClient(env.app) as client:
        while True:
            params = {"limit": 3, "actor": "x"}
            if cursor:
                params["cursor"] = cursor
            page = client.get(
                "/registry/v1/audit", params=params, headers=_as("admin")
            ).json()
            seen += [e["action"] for e in page["events"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        bad = client.get(
            "/registry/v1/audit", params={"cursor": "abc"}, headers=_as("admin")
        )
    assert seen == ["a4", "a3", "a2", "a1", "a0"]
    assert bad.status_code == 400


# --- AuditObserver -----------------------------------------------------------


def test_audit_observer_records_execution_and_outcome():
    log = InMemoryAuditLog()
    observer = AuditObserver(audit_log=log)
    job = Job(id="job-1", user_id="u-1", process_id="srv:echo", provider="srv")

    def info(status):
        return JobStatusInfo(jobID="job-1", status=status, type="process")

    async def scenario():
        await observer.on_job_created(job, info(StatusCode.accepted))
        await observer.on_status_changed(job, None, info(StatusCode.running))
        await observer.on_job_completed(job, info(StatusCode.failed))

    asyncio.run(scenario())
    created, completed = log.records
    assert (created.action, created.actor_sub, created.job_id) == (
        "job.execute",
        "u-1",
        "job-1",
    )
    assert created.details == {"process_id": "srv:echo", "provider": "srv"}
    assert completed.action == "job.complete"
    assert completed.outcome == "failure"
    assert completed.details["status"] == "failed"
