"""Onboarding end to end on the real adapters: SQL registry (PostgreSQL), real
AioHttpSafeFetch against a live local OGC server, Fernet cipher, router.

Flow: register server → discover → create drafts → edit card, then check the
rows and the audit log in the database. Runs only with UMP_TEST_DATABASE_URL.
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
from ump.core.services.authorization import AuthorizationService

DB_URL = os.environ.get("UMP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="UMP_TEST_DATABASE_URL not set (needs PostgreSQL)"
)
REPO_ROOT = Path(__file__).resolve().parents[1]
OWNER = AuthContext("owner-1", ["provider"], True, name="Olga")


class OwnerAuth(AuthPort):
    async def verify(self, token):
        return OWNER if token else AuthContext(None, [], False)


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
        auth_port=OwnerAuth(),
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
        ),
    )
    headers = {"Authorization": "Bearer owner"}
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
    finally:
        asyncio.run(engine.dispose())

    (model,) = _sql(
        "SELECT status, license, owner_sub, current_interface_sha256 "
        "FROM registry.model"
    )
    assert model.status == "draft" and model.license == "MIT"
    assert model.owner_sub == "owner-1" and len(model.current_interface_sha256) == 64
    actions = [r[0] for r in _sql("SELECT action FROM registry.audit_log ORDER BY id")]
    assert actions == [
        "server.create",
        "server.discover",
        "model.create",
        "model.card.update",
    ]
    (user,) = _sql("SELECT display_name FROM registry.app_user WHERE sub = 'owner-1'")
    assert user.display_name == "Olga"
