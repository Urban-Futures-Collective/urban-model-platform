"""Unit tests around registry phase A that need no HTTP:

- ProcessManager's process-list cache follows the provider configuration
  (a newly published model appears in /v1.0/processes at once);
- RegistryAccountService.touch is throttled and never fails a request;
- language helpers; English is the default language.
"""

from typing import Any, Dict, List
from uuid import uuid4

import pytest

from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.providers_config import ProviderConfig, ProvidersSnapshot
from ump.core.models.registry.entities import RegistryModel
from ump.core.models.registry.localized import (
    parse_accept_language,
    pick_language,
    resolve,
)
from ump.core.services.registry_accounts import RegistryAccountService
from ump.core.settings import UmpSettings

URL = "http://srv.local/"


class MutableProviders(ProvidersPort):
    def __init__(self, processes: List[Dict[str, Any]]):
        self.set(processes)

    def set(self, processes):
        self._snapshot = ProvidersSnapshot(
            [
                ProviderConfig.model_validate(
                    {"name": "srv", "url": URL, "processes": processes}
                )
            ]
        )

    def load_providers(self): ...

    def get_providers(self):
        return self._snapshot.providers

    def get_provider(self, name):
        return self._snapshot.get_provider(name)

    def get_process_config(self, name, pid):
        return self._snapshot.get_process_config(name, pid)

    def list_providers(self):
        return self._snapshot.provider_names()

    def get_processes(self, name):
        return self._snapshot.get_processes(name)

    def check_process_availability(self, name, pid):
        return self._snapshot.check_process_availability(name, pid)


class CountingHttp(HttpClientPort):
    def __init__(self):
        self.gets: List[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, timeout=None, headers=None):
        self.gets.append(url)
        pid = url.rsplit("/", 1)[-1]
        return {"id": pid, "title": pid, "version": "1", "inputs": {}, "outputs": {}}

    async def get_content(self, url, timeout=None, headers=None):
        raise AssertionError

    async def post(self, url, json=None, timeout=None, headers=None):
        raise AssertionError

    async def close(self):
        return None


def _ids(process_list) -> List[str]:
    return sorted(p.pid for p in process_list.processes)


@pytest.mark.asyncio
async def test_process_list_cache_follows_the_configuration():
    providers = MutableProviders([{"id": "a"}])
    http = CountingHttp()
    pm = ProcessManager(providers, http, process_id_validator=ColonProcessId())

    assert _ids(await pm.get_all_processes()) == ["srv:a"]
    assert _ids(await pm.get_all_processes()) == ["srv:a"]
    assert len(http.gets) == 1  # second call served from cache

    providers.set([{"id": "a"}, {"id": "b"}])  # e.g. a model was published
    assert _ids(await pm.get_all_processes()) == ["srv:a", "srv:b"]

    providers.set([{"id": "a"}, {"id": "b", "state": "deactivated"}])
    assert _ids(await pm.get_all_processes()) == ["srv:a"]

    calls = len(http.gets)
    assert _ids(await pm.get_all_processes()) == ["srv:a"]
    assert len(http.gets) == calls  # unchanged configuration: cached again


@pytest.mark.asyncio
async def test_touch_is_throttled_and_skips_anonymous():
    now = [0.0]
    repo = InMemoryRegistryRepository()
    accounts = RegistryAccountService(repo, touch_interval=60, monotonic=lambda: now[0])
    alice = AuthContext("alice", [], True, name="Alice")

    await accounts.touch(AuthContext(None, [], False))
    assert repo.users == {}

    await accounts.touch(alice)
    first = repo.users["alice"].last_seen_at
    await accounts.touch(AuthContext("alice", [], True, name="Renamed"))
    assert repo.users["alice"].display_name == "Alice"  # throttled

    now[0] = 61
    await accounts.touch(AuthContext("alice", [], True, name="Renamed"))
    assert repo.users["alice"].display_name == "Renamed"
    assert repo.users["alice"].last_seen_at >= first


@pytest.mark.asyncio
async def test_touch_never_fails_the_request():
    class Broken(InMemoryRegistryRepository):
        async def upsert_user(self, user):
            raise RuntimeError("database down")

    await RegistryAccountService(Broken()).touch(AuthContext("a", [], True))


def test_language_helpers():
    assert parse_accept_language("en;q=0.5, de-CH, *;q=0.1, fr;q=0") == ["de-CH", "en"]
    assert parse_accept_language(None) == []
    assert pick_language(["de-CH"], ["de", "en"], "en") == "de"
    assert pick_language(["fr"], ["de", "en"], "en") == "en"
    assert resolve({"de": "x"}, "en", "de") == "x"
    assert resolve({}, "en", "de") is None


def test_english_is_the_default_language(monkeypatch):
    monkeypatch.delenv("UMP_REGISTRY_LANGUAGES", raising=False)
    assert UmpSettings(_env_file=None).UMP_REGISTRY_LANGUAGES[0] == "en"
    model = RegistryModel(
        slug="m",
        provider_server_id=uuid4(),
        remote_process_id="p",
        owner_sub="o",
        full_name={"en": "M"},
    )
    assert model.default_lang == "en"
