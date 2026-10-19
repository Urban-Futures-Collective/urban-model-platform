"""ProvidersImportService with in-test adapters (no network, no database)."""

import json
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository
from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.credential_cipher import CredentialCipherPort
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.remote_auth import ProviderCredentials, RemoteAuthPort
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.providers_config import ProviderConfig
from ump.core.models.registry import IMPORT_ACTOR_SUB, interface_sha256
from ump.core.services.providers_import import (
    PLACEHOLDER_LICENSE,
    ProvidersImportService,
)

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


class DescriptionsHttp(HttpClientPort):
    """Serves process descriptions by URL; records request headers."""

    def __init__(self, descriptions: Dict[str, Dict[str, Any]]):
        self.descriptions = descriptions
        self.headers: Dict[str, Any] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, timeout=None, headers=None):
        self.headers[url] = headers
        if url not in self.descriptions:
            raise OGCProcessException(
                OGCExceptionResponse(type="about:blank", title="Not Found", status=404)
            )
        return self.descriptions[url]

    async def post(self, url, json, timeout=None, headers=None):
        raise AssertionError("import never POSTs")

    async def get_content(self, url, timeout=None, headers=None):
        raise AssertionError("not used")

    async def close(self):
        return None


class TokenAuth(RemoteAuthPort):
    def resolve(self, auth_config):
        if auth_config.type == "BearerToken":
            token = auth_config.token.get_secret_value()
            return ProviderCredentials(headers={"Authorization": f"Bearer {token}"})
        return ProviderCredentials()


class ReversingCipher(CredentialCipherPort):
    def encrypt(self, plaintext: str) -> bytes:
        return plaintext[::-1].encode()

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode()[::-1]


def _description(pid: str, title: str) -> Dict[str, Any]:
    return {
        "id": pid,
        "title": title,
        "description": f"{title} does things",
        "keywords": ["a", "b"],
        "inputs": {"x": {"schema": {"type": "number"}}},
        "outputs": {"y": {"schema": {"type": "number"}}},
    }


PROVIDERS = [
    ProviderConfig.model_validate(
        {
            "name": "srv",
            "url": "http://srv.local",
            "ttw-job-done": 60,
            "authentication": {"type": "BearerToken", "token": "tok"},
            "processes": [
                {"id": "echo", "description": "Echo model", "anonymous-access": True},
                {"id": "slow", "poll-interval": 2.5, "version": "1.2"},
                {"id": "off", "exclude": True},
                {"id": "broken"},
            ],
        }
    ),
    ProviderConfig.model_validate(
        {"name": "Bad_Name", "url": "http://bad.local", "processes": [{"id": "p"}]}
    ),
]
DESCRIPTIONS = {
    "http://srv.local/processes/echo": _description("echo", "Echo"),
    "http://srv.local/processes/slow": _description("slow", "Slow"),
}


def _service(registry, cipher=ReversingCipher(), http=None):
    return ProvidersImportService(
        registry,
        http or DescriptionsHttp(DESCRIPTIONS),
        TokenAuth(),
        cipher,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_import_creates_published_verified_models():
    registry = InMemoryRegistryRepository()
    http = DescriptionsHttp(DESCRIPTIONS)
    report = await _service(registry, http=http).import_providers(PROVIDERS, "admin")

    assert report.servers_created == ["srv"]
    assert report.models_created == ["srv:echo", "srv:slow"]
    assert sorted(report.failures)[0].startswith("Bad_Name:")
    assert any(f.startswith("srv:broken:") for f in report.failures)
    assert not report.ok

    server = registry.servers["srv"]
    assert server.auth_type == "BearerToken"
    assert server.ttw_job_done_seconds == 60
    assert json.loads(ReversingCipher().decrypt(server.credentials)) == {"token": "tok"}
    # descriptions were fetched with the provider's outbound credentials
    assert http.headers["http://srv.local/processes/echo"] == {
        "Authorization": "Bearer tok"
    }

    echo = registry.models[(server.id, "echo")]
    assert echo.status == "published" and echo.owner_sub == "admin"
    assert echo.verified_by == IMPORT_ACTOR_SUB != echo.owner_sub  # four-eyes
    assert echo.verified_interface_sha256 == interface_sha256(
        DESCRIPTIONS["http://srv.local/processes/echo"]
    )
    assert echo.full_name == {"de": "Echo model"}  # providers.yaml description wins
    assert echo.purpose == {"de": "Echo does things"}
    assert echo.license == PLACEHOLDER_LICENSE
    assert echo.slug == "srv-echo"
    assert echo.process_config == {
        "anonymous-access": True,
        "deterministic": False,
        "result-storage": "remote",
    }

    slow = registry.models[(server.id, "slow")]
    assert slow.full_name == {"de": "Slow"}  # falls back to the remote title
    assert slow.version_label == "1.2"
    assert slow.process_config["poll-interval"] == 2.5

    assert (server.id, "off") not in registry.models
    assert {"admin", IMPORT_ACTOR_SUB} <= set(registry.users)


@pytest.mark.asyncio
async def test_reimport_is_create_only():
    registry = InMemoryRegistryRepository()
    await _service(registry).import_providers(PROVIDERS, "admin")
    server_id = registry.servers["srv"].id
    deactivated = registry.models[(server_id, "echo")].model_copy(
        update={"status": "deactivated"}
    )
    registry.models[(server_id, "echo")] = deactivated

    # the broken process is fixed on the server side and the import re-run
    fixed = {
        **DESCRIPTIONS,
        "http://srv.local/processes/broken": _description("broken", "Broken"),
    }
    report = await _service(registry, http=DescriptionsHttp(fixed)).import_providers(
        PROVIDERS[:1], "admin"
    )
    assert report.ok
    assert report.servers_existing == ["srv"]
    assert report.models_existing == ["srv:echo", "srv:slow"]
    assert report.models_created == ["srv:broken"]
    # a registry change since the first import is not undone
    assert registry.models[(server_id, "echo")].status == "deactivated"


@pytest.mark.asyncio
async def test_credentials_need_a_cipher():
    registry = InMemoryRegistryRepository()
    report = await _service(registry, cipher=None).import_providers(
        PROVIDERS[:1], "admin"
    )
    assert report.servers_created == []
    assert "UMP_REGISTRY_CREDENTIALS_KEY" in report.failures[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["", IMPORT_ACTOR_SUB])
async def test_owner_must_be_a_real_account(owner):
    with pytest.raises(ValueError):
        await _service(InMemoryRegistryRepository()).import_providers(PROVIDERS, owner)
