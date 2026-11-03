"""GET /jobs lists only the caller's own jobs (issue #24).

Anonymous jobs are not tied to a caller, so they are never listed while
auth is enabled; they stay reachable by their ID.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.web.fastapi import create_app
from ump.core.interfaces.auth import AuthContext, AuthPort
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.settings import app_settings


class TokenAuth(AuthPort):
    """Resolves the bearer token to a user of the same name."""

    async def verify(self, token):
        if not token:
            return AuthContext(user_id=None, roles=[], is_authenticated=False)
        return AuthContext(user_id=token, roles=[], is_authenticated=True)


class FakeHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def close(self):
        return None


class StubJobManager:
    async def shutdown(self):
        return None


def _job(job_id: str, user_id: str | None) -> Job:
    return Job(
        id=job_id,
        user_id=user_id,
        status=str(StatusCode.successful),
        status_info=JobStatusInfo(jobID=job_id, status=StatusCode.successful),
    )


@pytest.fixture(autouse=True)
def auth_enabled(monkeypatch):
    monkeypatch.setattr(app_settings, "UMP_AUTH_ENABLED", True)


@pytest.fixture
def client():
    repo = InMemoryJobRepository()
    for job in (_job("anon-1", None), _job("alice-1", "alice"), _job("bob-1", "bob")):
        asyncio.run(repo.create(job))
    app = create_app(
        process_manager_factory=lambda http_client: object(),
        http_client=FakeHttpClient(),
        job_manager_factory=lambda http_client, process_manager: StubJobManager(),
        job_repo=repo,
        auth_port=TokenAuth(),
    )
    with TestClient(app) as test_client:
        yield test_client


def _listed_ids(client, headers=None) -> set[str]:
    response = client.get("/v1.0/jobs", headers=headers or {})
    assert response.status_code == 200
    return {job["jobID"] for job in response.json()["jobs"]}


def test_list_jobs_returns_nothing_when_unauthenticated(client):
    assert _listed_ids(client) == set()


def test_list_jobs_returns_only_own_jobs_when_authenticated(client):
    assert _listed_ids(client, {"Authorization": "Bearer alice"}) == {"alice-1"}


def test_get_job_keeps_anonymous_job_reachable_by_id(client):
    assert client.get("/v1.0/jobs/anon-1").status_code == 200


def test_list_jobs_returns_anonymous_jobs_when_auth_disabled(client, monkeypatch):
    monkeypatch.setattr(app_settings, "UMP_AUTH_ENABLED", False)

    assert _listed_ids(client) == {"anon-1"}
