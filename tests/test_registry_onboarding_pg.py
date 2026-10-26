"""Registry end to end on the real adapters: SQL registry (PostgreSQL), real
AioHttpSafeFetch against a live local OGC server, Fernet cipher, router.

Flow: register server → discover → create drafts → edit card → submit →
review → verify (with a real job) → publish, then check the rows, the audit
log and the provider view in the database — so every write is checked against
the 0003 CHECK constraints. Runs only with UMP_TEST_DATABASE_URL.
"""

import asyncio
import os
import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from aiohttp import web
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.adapters.credential_cipher_fernet import FernetCredentialCipher
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.registry_repository_sql import SQLModelRegistryRepository
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
from ump.adapters.safe_fetch_aiohttp import AioHttpSafeFetch
from ump.adapters.web.fastapi import create_app
from ump.adapters.web.registry_router import RegistryServices
from ump.core.interfaces.auth import AuthContext, AuthPort
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.job import Job
from ump.core.services.authorization import AuthorizationService

DB_URL = os.environ.get("UMP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="UMP_TEST_DATABASE_URL not set (needs PostgreSQL)"
)
REPO_ROOT = Path(__file__).resolve().parents[1]
CALLERS = {
    "owner": AuthContext("owner-1", ["provider"], True, name="Olga"),
    "verifier": AuthContext("ver-1", ["verifier"], True),
}


class TokenAuth(AuthPort):
    async def verify(self, token):
        return CALLERS[token] if token else AuthContext(None, [], False)


@contextmanager
def ogc_server() -> Iterator[str]:
    """A tiny OGC API Processes server on a free port, in its own thread."""

    async def processes(request):
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        return web.json_response({"processes": [{"id": "echo", "title": "Echo"}]})

    async def process(request):
        title = {"de": "Echo (de)", "en": "Echo"}[request.headers["Accept-Language"]]
        return web.json_response(
            {"id": "echo", "title": title, "inputs": {}, "outputs": {}}
        )

    app = web.Application()
    app.router.add_get("/processes", processes)
    app.router.add_get("/processes/echo", process)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    started = threading.Event()

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started.wait(5)
    try:
        yield f"http://localhost:{port}"
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


@pytest.fixture
def database():
    from alembic import command
    from alembic.config import Config

    os.environ["UMP_DATABASE_URL"] = DB_URL
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    yield
    _sql(
        "TRUNCATE registry.model, registry.provider_server, registry.app_user, "
        "registry.audit_log"
    )


def _sql(query: str):
    async def run():
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(query))
                return result.all() if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_onboarding_flow_on_real_adapters(database):
    _sql(
        "TRUNCATE registry.model, registry.provider_server, registry.app_user, "
        "registry.audit_log"
    )
    engine = create_async_engine(DB_URL)
    jobs = InMemoryJobRepository("scratch/test_registry_onboarding_pg_jobs")
    repo = SQLModelRegistryRepository(
        async_sessionmaker(engine, expire_on_commit=False)
    )
    authz = AuthorizationService(None)  # type: ignore[arg-type]
    validator = ColonProcessId()

    class NoProviders:
        def load_providers(self):
            pass

    class _StubJobManager:
        async def shutdown(self):
            return None

    class _Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    app = create_app(
        process_manager_factory=lambda c: ProcessManager(
            NoProviders(), c, process_id_validator=validator  # type: ignore[arg-type]
        ),
        http_client=_Http(),  # type: ignore[arg-type]
        job_manager_factory=lambda c, pm: _StubJobManager(),
        job_repo=InMemoryJobRepository("scratch/test_registry_onboarding_pg"),
        process_id_validator=validator,
        auth_port=TokenAuth(),
        authorization_service=authz,
        registry=RegistryServices.create(
            repo,
            authorization=authz,
            providers=NoProviders(),  # type: ignore[arg-type]
            fetch=AioHttpSafeFetch(
                allow_http=True, allowed_private_hosts=["localhost"]
            ),
            remote_auth=RemoteAuthAdapter(),
            cipher=FernetCredentialCipher([Fernet.generate_key().decode()]),
            languages=["de", "en"],
            public_catalogue=False,
            jobs=jobs,
        ),
    )
    headers = {"Authorization": "Bearer owner"}
    reviewer = {"Authorization": "Bearer verifier"}
    try:
        with ogc_server() as base, TestClient(app) as client:
            created = client.post(
                "/registry/v1/servers",
                headers=headers,
                json={
                    "name": "echo-srv",
                    "base_url": base,
                    "authentication": {"type": "BearerToken", "token": "tok"},
                },
            )
            assert created.status_code == 201, created.text
            sid = created.json()["id"]

            found = client.post(f"/registry/v1/servers/{sid}/discover", headers=headers)
            assert found.status_code == 200, found.text
            assert [p["id"] for p in found.json()["processes"]] == ["echo"]

            drafts = client.post(
                f"/registry/v1/servers/{sid}/models",
                headers=headers,
                json={"remote_process_ids": ["echo"]},
            )
            assert drafts.status_code == 201, drafts.text
            model_id = drafts.json()["created"][0]["id"]

            etag = client.get(
                f"/registry/v1/models/{model_id}", headers=headers
            ).headers["ETag"]
            edited = client.patch(
                f"/registry/v1/models/{model_id}",
                headers={**headers, "If-Match": etag},
                json={"license": "MIT", "purpose": {"de": "Test"}},
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["full_name"] == {"de": "Echo (de)", "en": "Echo"}

            # phase C: review workflow on the real database (CHECKs apply)
            def step(who, to, **extra):
                r = client.post(
                    f"/registry/v1/models/{model_id}/transitions",
                    headers=who,
                    json={"to_status": to, **extra},
                )
                assert r.status_code == 200, r.text
                return r

            step(headers, "submitted")
            step(reviewer, "in_review")
            asyncio.run(
                jobs.create(
                    Job(
                        id="verify-1",
                        user_id="ver-1",
                        process_id="echo-srv:echo",
                        provider="echo-srv",
                        status="successful",
                    )
                )
            )
            step(reviewer, "verified", verification_job_id="verify-1")
            step(reviewer, "published")
    finally:
        asyncio.run(engine.dispose())

    (model,) = _sql(
        "SELECT status, exec_state, license, owner_sub, verified_by, "
        "verified_interface_sha256, current_interface_sha256, published_at "
        "FROM registry.model"
    )
    assert model.status == "published" and model.exec_state == "published"
    assert model.license == "MIT" and model.owner_sub == "owner-1"
    assert model.verified_by == "ver-1" and model.published_at is not None
    assert model.verified_interface_sha256 == model.current_interface_sha256
    # what the OGC side reads now contains the published model
    (row,) = _sql("SELECT name, processes FROM registry.v_providers_config")
    assert row.name == "echo-srv" and row.processes[0]["state"] == "published"
    actions = [r[0] for r in _sql("SELECT action FROM registry.audit_log ORDER BY id")]
    assert actions == [
        "server.create",
        "server.discover",
        "model.create",
        "model.card.update",
        "model.status.change",
        "model.status.change",
        "model.status.change",
        "model.status.change",
    ]
    (user,) = _sql("SELECT display_name FROM registry.app_user WHERE sub = 'owner-1'")
    assert user.display_name == "Olga"
