"""Tests for the MCP tool catalog (Feature X, phase 1).

The unit under test is the core service `ToolCatalogService`, exercised against
a real `ProcessManager` and a real `AuthorizationService` so that role filtering
and process fetching behave exactly as they do in production. Only the outward
ports are faked:

    - ProvidersPort   : `FakeProvidersService` (list-based providers.yaml shape)
    - HttpClientPort  : `FakeHttpClient` (canned OGC process descriptions)

Scenarios (mcp-integration-strategy.md §11, reports/REF-F10-mcp-endpoint.md):
    1. Anonymous caller sees only `anonymous-access: true` processes.
    2. Role matrix: no token / token without role / provider role / process role.
    3. Parity: the catalog is a subset of what GET /processes lists.
    4. Failure isolation: one unreachable process does not empty the catalog.
    5. Descriptor shape: inputSchema, provider/processId split, title fallback.
    6. `unrestricted=True` bypasses filtering (UMP_AUTH_ENABLED=false).
"""

from typing import Any, Dict, List, cast

import pytest

from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.core.interfaces.auth import AuthContext, AuthPort
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.providers_config import ProviderConfig
from ump.core.services.authorization import AuthorizationService
from ump.core.services.tool_catalog import ToolCatalogService

PROVIDER_URL = "http://provider.local/"


def _process_doc(pid: str, title: str, inputs: Dict[str, Any] | None = None):
    return {
        "id": pid,
        "title": title,
        "description": f"{title} description",
        "version": "1.0",
        "jobControlOptions": ["async-execute"],
        "outputTransmission": ["value"],
        "inputs": inputs if inputs is not None else {},
        "outputs": {},
        "links": [],
    }


class FakeProvidersService(ProvidersPort):
    """One provider, `open` is anonymous-accessible and `secret` is not."""

    def __init__(self, processes: List[Dict[str, Any]] | None = None):
        self._processes = processes if processes is not None else [
            {"id": "open", "anonymous-access": True},
            {"id": "secret"},
        ]

    def load_providers(self) -> None:
        return None

    def get_providers(self) -> List[ProviderConfig]:
        return [self.get_provider("infra")]

    def get_provider(self, provider_name: str) -> ProviderConfig:
        return ProviderConfig.model_validate(
            {
                "name": "infra",
                "url": PROVIDER_URL,
                "processes": self._processes,
            }
        )

    def get_process_config(self, provider_name: str, process_id: str):
        raise NotImplementedError

    def list_providers(self) -> List[str]:
        return ["infra"]

    def get_processes(self, provider_name: str) -> List[str]:
        return [p["id"] for p in self._processes]

    def check_process_availability(self, provider_name: str, process_id: str) -> bool:
        return True


class FakeHttpClient(HttpClientPort):
    def __init__(self, responses: Dict[str, Any]):
        self._responses = responses
        self.requests: List[str] = []

    async def __aenter__(self) -> HttpClientPort:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def get(
        self, url: str, timeout: float | None = None, headers: dict | None = None
    ) -> Dict[str, Any]:
        self.requests.append(url)
        if url in self._responses:
            value = self._responses[url]
            if isinstance(value, Exception):
                raise value
            return cast(Dict[str, Any], value)
        raise RuntimeError(f"no response registered for {url}")

    async def get_content(
        self, url: str, timeout: float | None = None, headers: dict | None = None
    ) -> tuple[bytes, str]:
        return b"", "application/json"

    async def close(self) -> None:
        return None

    async def post(
        self,
        url: str,
        json: Dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError


def build_service(
    responses: Dict[str, Any] | None = None,
    processes: List[Dict[str, Any]] | None = None,
):
    providers = FakeProvidersService(processes)
    default_responses = {
        f"{PROVIDER_URL}processes/open": _process_doc(
            "open",
            "Open Process",
            {
                "name": {
                    "title": "Name",
                    "description": "Who to greet",
                    "schema": {"type": "string"},
                    "minOccurs": 1,
                }
            },
        ),
        f"{PROVIDER_URL}processes/secret": _process_doc("secret", "Secret Process"),
    }
    client = FakeHttpClient(responses if responses is not None else default_responses)
    validator = ColonProcessId()
    process_manager = ProcessManager(
        providers, client, process_id_validator=validator
    )
    authz = AuthorizationService(providers)
    service = ToolCatalogService(
        process_manager=process_manager,
        authorization_service=authz,
        process_id_validator=validator,
    )
    return service, process_manager


def anonymous() -> AuthContext:
    return AuthContext(user_id=None, roles=[], is_authenticated=False)


def user(*roles: str) -> AuthContext:
    return AuthContext(user_id="u1", roles=list(roles), is_authenticated=True)


# ---------------------------------------------------------------------------
# 1 + 2. Role matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_sees_only_anonymous_access_processes():
    service, _ = build_service()

    catalog = await service.build(anonymous())

    assert [t.tool for t in catalog.tools] == ["infra:open"]


@pytest.mark.asyncio
async def test_authenticated_without_roles_sees_only_anonymous_processes():
    service, _ = build_service()

    catalog = await service.build(user())

    assert [t.tool for t in catalog.tools] == ["infra:open"]


@pytest.mark.asyncio
async def test_provider_role_grants_every_process_of_that_provider():
    service, _ = build_service()

    catalog = await service.build(user("infra"))

    assert [t.tool for t in catalog.tools] == ["infra:open", "infra:secret"]


@pytest.mark.asyncio
async def test_process_role_grants_exactly_one_process():
    service, _ = build_service()

    catalog = await service.build(user("infra:secret"))

    assert [t.tool for t in catalog.tools] == ["infra:open", "infra:secret"]


@pytest.mark.asyncio
async def test_unrelated_role_grants_nothing_extra():
    service, _ = build_service()

    catalog = await service.build(user("other-provider"))

    assert [t.tool for t in catalog.tools] == ["infra:open"]


@pytest.mark.asyncio
async def test_empty_catalog_is_valid_not_an_error():
    """No anonymous-access processes configured → anonymous caller gets zero tools."""
    service, _ = build_service(processes=[{"id": "secret"}])

    catalog = await service.build(anonymous())

    assert catalog.tools == []
    assert catalog.version == "1.0"


# ---------------------------------------------------------------------------
# 3. Parity with GET /processes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_is_a_subset_of_the_process_list():
    service, process_manager = build_service()

    catalog = await service.build(user("infra"))
    process_list = await process_manager.get_all_processes()

    listed = {p.pid for p in process_list.processes}
    assert {t.tool for t in catalog.tools} <= listed


# ---------------------------------------------------------------------------
# 4. Failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_process_is_skipped_not_fatal():
    responses = {
        f"{PROVIDER_URL}processes/open": _process_doc("open", "Open Process"),
        f"{PROVIDER_URL}processes/secret": RuntimeError("provider down"),
    }
    service, _ = build_service(responses=responses)

    catalog = await service.build(user("infra"))

    assert [t.tool for t in catalog.tools] == ["infra:open"]


# ---------------------------------------------------------------------------
# 5. Descriptor shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_descriptor_carries_input_schema_and_split_ids():
    service, _ = build_service()

    catalog = await service.build(user("infra"))
    tool = next(t for t in catalog.tools if t.tool == "infra:open")

    assert tool.provider == "infra"
    assert tool.processId == "open"
    assert tool.title == "Open Process"
    assert tool.inputSchema["type"] == "object"
    assert tool.inputSchema["properties"]["name"]["type"] == "string"
    assert tool.inputSchema["required"] == ["name"]


@pytest.mark.asyncio
async def test_process_without_inputs_gets_empty_object_schema():
    service, _ = build_service()

    catalog = await service.build(user("infra"))
    tool = next(t for t in catalog.tools if t.tool == "infra:secret")

    assert tool.inputSchema == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# 6. Auth disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrestricted_bypasses_role_filtering():
    """UMP_AUTH_ENABLED=false must not hide every non-anonymous process."""
    service, _ = build_service()

    catalog = await service.build(anonymous(), unrestricted=True)

    assert [t.tool for t in catalog.tools] == ["infra:open", "infra:secret"]


# ---------------------------------------------------------------------------
# 7. Route mounting
# ---------------------------------------------------------------------------


class FakeAuthPort(AuthPort):
    """Always resolves to the same context, whatever the token."""

    def __init__(self, context: AuthContext):
        self._context = context

    async def verify(self, token):
        return self._context


def _build_app(auth_port: AuthPort | None = None):
    """Wire a real app the way the composition root does, with faked ports."""
    from fastapi.testclient import TestClient

    from ump.adapters.job_repository_inmemory import InMemoryJobRepository
    from ump.adapters.web.fastapi import create_app

    providers = FakeProvidersService()
    client = FakeHttpClient(
        {
            f"{PROVIDER_URL}processes/open": _process_doc("open", "Open Process"),
            f"{PROVIDER_URL}processes/secret": _process_doc("secret", "Secret"),
        }
    )
    validator = ColonProcessId()
    authz = AuthorizationService(providers)

    def process_manager_factory(http_client):
        return ProcessManager(providers, http_client, process_id_validator=validator)

    def job_manager_factory(http_client, process_manager):
        class _StubJobManager:
            async def shutdown(self):
                return None

        return _StubJobManager()

    def tool_catalog_factory(process_manager):
        return ToolCatalogService(
            process_manager=process_manager,
            authorization_service=authz,
            process_id_validator=validator,
        )

    app = create_app(
        process_manager_factory=process_manager_factory,
        http_client=client,
        job_manager_factory=job_manager_factory,
        job_repo=InMemoryJobRepository("scratch/test_tool_catalog"),
        process_id_validator=validator,
        auth_port=auth_port,
        authorization_service=authz,
        tool_catalog_factory=tool_catalog_factory,
    )
    return TestClient(app)


def test_catalog_is_mounted_under_its_own_version_prefix():
    with _build_app() as client:
        assert client.get("/mcp/v1/tools").status_code == 200
        # No floating "latest" alias: a client must name the contract it wants,
        # so a future /mcp/v2 cannot silently break a pinned consumer.
        assert client.get("/mcp/tools").status_code == 404


def test_catalog_prefix_is_independent_of_the_ogc_version():
    with _build_app() as client:
        assert client.get("/v1.0/mcp/tools").status_code == 404
        assert client.get("/v1.0/mcp/v1/tools").status_code == 404


def test_route_reports_contract_revision_in_the_body():
    with _build_app() as client:
        body = client.get("/mcp/v1/tools").json()

    assert body["version"] == "1.0"


def test_no_auth_port_means_auth_is_not_in_play():
    """Nothing wired to authenticate against → nothing to filter on."""
    with _build_app() as client:
        body = client.get("/mcp/v1/tools").json()

    assert [t["tool"] for t in body["tools"]] == ["infra:open", "infra:secret"]


def test_route_filters_but_never_401s_an_anonymous_caller(monkeypatch):
    from ump.core.settings import app_settings

    monkeypatch.setattr(app_settings, "UMP_AUTH_ENABLED", True)

    with _build_app(auth_port=FakeAuthPort(anonymous())) as client:
        response = client.get("/mcp/v1/tools")

    assert response.status_code == 200
    assert [t["tool"] for t in response.json()["tools"]] == ["infra:open"]


def test_auth_disabled_bypasses_filtering_over_http(monkeypatch):
    """UMP_AUTH_ENABLED=false: the switch means what settings.py says it means."""
    from ump.core.settings import app_settings

    monkeypatch.setattr(app_settings, "UMP_AUTH_ENABLED", False)

    with _build_app(auth_port=FakeAuthPort(anonymous())) as client:
        body = client.get("/mcp/v1/tools").json()

    assert [t["tool"] for t in body["tools"]] == ["infra:open", "infra:secret"]
