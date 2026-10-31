"""Registry API phase B over HTTP: servers, discovery, draft models, card edits.

In-memory registry, a real Fernet cipher and an in-test SafeFetchPort that
serves OGC descriptions (optionally per Accept-Language). Covers roles and
ownership, If-Match, write-only credentials, audit events, provider snapshot
reloads and the card rules.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ump.adapters.audit_log_inmemory import InMemoryAuditLog
from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.adapters.credential_cipher_fernet import FernetCredentialCipher
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
from ump.adapters.web.fastapi import create_app
from ump.adapters.web.registry_router import RegistryServices
from ump.core.interfaces.auth import AuthContext, AuthPort
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.safe_fetch import (
    RemoteFetchError,
    SafeFetchPort,
    UnsafeUrlError,
)
from ump.core.managers.process_manager import ProcessManager
from ump.core.services.authorization import AuthorizationService

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
BASE = "https://models.example.org/"

CALLERS: Dict[str, AuthContext] = {
    "owner": AuthContext("owner-1", ["provider"], True),
    "other": AuthContext("owner-2", ["provider"], True),
    "admin": AuthContext("adm-1", ["platform-admin"], True),
    "verifier": AuthContext("ver-1", ["verifier"], True),
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


def _process(pid: str, title: str, **extra) -> Dict[str, Any]:
    return {
        "id": pid,
        "title": title,
        "description": f"{title} description",
        "version": "1.0",
        "keywords": ["bike", "network"],
        "inputs": {"x": {"title": "X", "schema": {"type": "number"}}},
        "outputs": {"y": {"schema": {"type": "number"}}},
        **extra,
    }


class FakeFetch(SafeFetchPort):
    """URL → payload; a dict keyed by language serves Accept-Language variants."""

    def __init__(self):
        self.payloads: Dict[str, Any] = {
            BASE
            + "processes": {
                "processes": [
                    {"id": "growbike", "title": "GrowBike", "version": "1.0"},
                    {"id": "fixbike", "title": "FixBike"},
                ]
            },
            BASE
            + "processes/growbike": {
                "de": _process("growbike", "Radnetz wachsen"),
                "en": _process("growbike", "Grow bike network"),
            },
            # ignores Accept-Language: same answer for every language
            BASE + "processes/fixbike": _process("fixbike", "FixBike"),
        }
        self.requests: List[tuple[str, Dict[str, str]]] = []

    def check_url(self, url):
        if "internal" in url:
            raise UnsafeUrlError("not a public address")

    async def get_json(self, url, headers=None):
        self.check_url(url)
        self.requests.append((url, dict(headers or {})))
        if url not in self.payloads:
            raise RemoteFetchError("HTTP 404")
        payload = self.payloads[url]
        if isinstance(payload, dict) and set(payload) <= {"de", "en"} and payload:
            return payload.get(
                (headers or {}).get("Accept-Language", "de"), payload["de"]
            )
        return payload


def _setup(with_cipher: bool = True, auth: bool = True):
    repo = InMemoryRegistryRepository(clock=lambda: T0)
    fetch = FakeFetch()
    providers = SpyProviders()
    cipher = (
        FernetCredentialCipher([Fernet.generate_key().decode()])
        if with_cipher
        else None
    )
    authz = AuthorizationService(providers)
    validator = ColonProcessId()

    class _StubJobManager:
        async def shutdown(self):
            return None

    app = create_app(
        process_manager_factory=lambda c: ProcessManager(
            providers, c, process_id_validator=validator
        ),
        http_client=NoHttp(),
        job_manager_factory=lambda c, pm: _StubJobManager(),
        job_repo=InMemoryJobRepository("scratch/test_registry_onboarding"),
        process_id_validator=validator,
        auth_port=TokenAuth() if auth else None,
        authorization_service=authz,
        registry=RegistryServices.create(
            repo,
            authorization=authz,
            providers=providers,
            fetch=fetch,
            remote_auth=RemoteAuthAdapter(),
            cipher=cipher,
            languages=["de", "en"],
            public_catalogue=True,
            jobs=InMemoryJobRepository("scratch/test_registry_jobs"),
            audit_log=InMemoryAuditLog(),
        ),
    )
    return TestClient(app), repo, fetch, providers, cipher


def _call(client, method, path, who=None, etag=None, **kw):
    headers = kw.pop("headers", {})
    if who:
        headers["Authorization"] = f"Bearer {who}"
    if etag:
        headers["If-Match"] = etag
    return client.request(method, f"/registry/v1{path}", headers=headers, **kw)


SERVER = {
    "name": "bikebox",
    "base_url": BASE.rstrip("/"),
    "authentication": {"type": "BasicAuth", "user": "alice", "password": "s3cret"},
}


def _create_server(client, who="owner", body=None):
    response = _call(client, "POST", "/servers", who, json=body or SERVER)
    assert response.status_code == 201, response.text
    return response


def _create_models(client, server_id, ids=("growbike", "fixbike"), who="owner"):
    response = _call(
        client,
        "POST",
        f"/servers/{server_id}/models",
        who,
        json={"remote_process_ids": list(ids)},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- servers ------------------------------------------------------------------


def test_create_server_encrypts_credentials_and_never_returns_them():
    client, repo, _, _, cipher = _setup()
    with client:
        response = _create_server(client)
        body = response.json()
        assert body["base_url"] == BASE  # trailing slash normalised
        assert body["owner_sub"] == "owner-1"
        assert body["auth_type"] == "BasicAuth" and body["credentials_set"] is True
        assert "s3cret" not in response.text and "alice" not in response.text
        assert response.headers["Location"] == f"/registry/v1/servers/{body['id']}"
        assert response.headers["ETag"]

        stored = repo.servers["bikebox"]
        assert json.loads(cipher.decrypt(stored.credentials)) == {
            "user": "alice",
            "password": "s3cret",
        }
        event = repo.audit[-1]
        assert event.action == "server.create" and event.actor_sub == "owner-1"
        assert "s3cret" not in json.dumps(event.model_dump(mode="json"))


@pytest.mark.parametrize(
    "who, body, status",
    [
        (None, SERVER, 401),
        ("viewer", SERVER, 403),
        ("verifier", SERVER, 403),
        ("owner", {**SERVER, "name": "Bad_Name"}, 400),
        ("owner", {**SERVER, "base_url": "https://internal.svc"}, 400),
        ("owner", {**SERVER, "unknown": 1}, 422),
    ],
)
def test_create_server_rejections(who, body, status):
    client, *_ = _setup()
    with client:
        assert _call(client, "POST", "/servers", who, json=body).status_code == status


def test_duplicate_name_is_409_and_missing_key_is_503():
    client, *_ = _setup()
    with client:
        _create_server(client)
        assert (
            _call(client, "POST", "/servers", "other", json=SERVER).status_code == 409
        )
    client, *_ = _setup(with_cipher=False)
    with client:
        assert (
            _call(client, "POST", "/servers", "owner", json=SERVER).status_code == 503
        )
        no_auth = {"name": "open", "base_url": BASE}
        assert (
            _call(client, "POST", "/servers", "owner", json=no_auth).status_code == 201
        )


def test_writes_are_rejected_when_auth_is_not_wired():
    client, *_ = _setup(auth=False)
    with client:
        assert _call(client, "POST", "/servers", json=SERVER).status_code == 401


def test_patch_server_needs_current_etag_and_ownership():
    client, repo, _, providers, _ = _setup()
    with client:
        created = _create_server(client)
        sid, etag = created.json()["id"], created.headers["ETag"]
        patch = {"ttw_job_done_seconds": 60}

        assert (
            _call(client, "PATCH", f"/servers/{sid}", "owner", json=patch).status_code
            == 428
        )
        assert (
            _call(
                client, "PATCH", f"/servers/{sid}", "owner", '"stale"', json=patch
            ).status_code
            == 412
        )
        assert (
            _call(
                client, "PATCH", f"/servers/{sid}", "other", etag, json=patch
            ).status_code
            == 404
        )
        assert (
            _call(
                client, "PATCH", f"/servers/{sid}", "owner", etag, json={"name": "x"}
            ).status_code
            == 422
        )

        reloads = providers.reloads
        ok = _call(client, "PATCH", f"/servers/{sid}", "owner", etag, json=patch)
        assert ok.status_code == 200 and ok.json()["ttw_job_done_seconds"] == 60
        assert ok.headers["ETag"] != etag
        assert providers.reloads == reloads + 1
        # the old ETag is now stale
        assert (
            _call(
                client, "PATCH", f"/servers/{sid}", "owner", etag, json=patch
            ).status_code
            == 412
        )

        # a platform admin may edit any server; null authentication = NoAuth
        current = _call(client, "GET", f"/servers/{sid}", "admin").headers["ETag"]
        cleared = _call(
            client,
            "PATCH",
            f"/servers/{sid}",
            "admin",
            current,
            json={"authentication": None},
        )
        assert cleared.json()["auth_type"] == "NoAuth"
        assert cleared.json()["credentials_set"] is False
        assert repo.servers["bikebox"].credentials is None
        assert repo.audit[-1].details["credentials"] == "removed"
        assert repo.audit[-1].details["auth_type"] == {
            "before": "BasicAuth",
            "after": "NoAuth",
        }


# --- discovery and drafts -----------------------------------------------------


def test_discover_lists_processes_with_outbound_credentials():
    client, repo, fetch, _, _ = _setup()
    with client:
        sid = _create_server(client).json()["id"]
        _create_models(client, sid, ids=("growbike",))

        result = _call(client, "POST", f"/servers/{sid}/discover", "owner").json()
        by_id = {p["id"]: p for p in result["processes"]}
        assert set(by_id) == {"growbike", "fixbike"}
        assert by_id["growbike"]["model_id"] is not None
        assert by_id["fixbike"]["model_id"] is None

        url, headers = fetch.requests[-1]
        assert url == BASE + "processes"
        assert headers["Authorization"].startswith("Basic ")
        assert repo.servers["bikebox"].last_discovered_at is not None
        assert repo.audit[-1].action == "server.discover"

        fetch.payloads.pop(BASE + "processes")
        assert (
            _call(client, "POST", f"/servers/{sid}/discover", "owner").status_code
            == 502
        )
        assert (
            _call(client, "POST", f"/servers/{sid}/discover", "other").status_code
            == 404
        )


def test_create_models_prefills_drafts_per_language():
    client, repo, _, _, _ = _setup()
    with client:
        sid = _create_server(client).json()["id"]
        result = _create_models(client, sid)
        assert [m["remote_process_id"] for m in result["created"]] == [
            "growbike",
            "fixbike",
        ]
        assert all(m["status"] == "draft" for m in result["created"])

        grow = next(
            m for m in repo.models.values() if m.remote_process_id == "growbike"
        )
        assert grow.full_name == {"de": "Radnetz wachsen", "en": "Grow bike network"}
        assert grow.short_description["en"] == "Grow bike network description"
        assert grow.keywords == {"de": ["bike", "network"]}
        assert grow.version_label == "1.0" and grow.owner_sub == "owner-1"
        assert grow.current_interface_sha256 and grow.slug == "bikebox-growbike"

        fix = next(m for m in repo.models.values() if m.remote_process_id == "fixbike")
        # the server ignores Accept-Language: no fake translation is stored
        assert fix.full_name == {"de": "FixBike"}

        events = [e for e in repo.audit if e.action == "model.create"]
        assert {e.model_id for e in events} == {grow.id, fix.id}

        again = _create_models(client, sid)
        assert again["created"] == [] and len(again["existing"]) == 2


@pytest.mark.parametrize(
    "who, body, status",
    [
        ("other", {"remote_process_ids": ["growbike"]}, 404),
        ("owner", {"remote_process_ids": ["growbike", "growbike"]}, 422),
        ("owner", {"remote_process_ids": []}, 422),
        ("owner", {"remote_process_ids": ["missing"]}, 502),
        ("owner", {"remote_process_ids": ["growbike"], "default_lang": "fr"}, 400),
    ],
)
def test_create_models_rejections(who, body, status):
    client, repo, *_ = _setup()
    with client:
        sid = _create_server(client).json()["id"]
        assert (
            _call(client, "POST", f"/servers/{sid}/models", who, json=body).status_code
            == status
        )
        assert repo.models == {}  # nothing half-created


def test_slug_collision_gets_a_suffix():
    client, repo, fetch, _, _ = _setup()
    with client:
        sid = _create_server(client).json()["id"]
        fetch.payloads[BASE + "processes/grow_bike"] = _process("grow_bike", "Other")
        _create_models(client, sid, ids=("growbike", "grow_bike"))
        slugs = sorted(m.slug for m in repo.models.values())
        assert slugs == ["bikebox-grow-bike", "bikebox-growbike"]
        fetch.payloads[BASE + "processes/grow-bike"] = _process("grow-bike", "Third")
        _create_models(client, sid, ids=("grow-bike",))
        assert "bikebox-grow-bike-2" in {m.slug for m in repo.models.values()}


# --- card edits ---------------------------------------------------------------


def _draft(client):
    sid = _create_server(client).json()["id"]
    model_id = _create_models(client, sid, ids=("growbike",))["created"][0]["id"]
    etag = _call(client, "GET", f"/models/{model_id}", "owner").headers["ETag"]
    return model_id, etag


def test_owner_edits_card_with_full_replacement_and_audit_diff():
    client, repo, *_ = _setup()
    with client:
        model_id, etag = _draft(client)
        patch = {
            "purpose": {"de": "Netzplanung", "en": "Network planning"},
            "license": "Apache-2.0",
            "keywords": {"en": ["cycling"]},
            "contributors": [{"name": "Ada", "role": "author"}],
            "applications": [{"title": {"de": "Hamburg"}, "year": 2025}],
            "process_config": {"anonymous-access": True, "poll-interval": 5},
        }
        response = _call(
            client, "PATCH", f"/models/{model_id}", "owner", etag, json=patch
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["lang"] == "*" and body["purpose"]["en"] == "Network planning"
        assert body["keywords"] == {"en": ["cycling"]}  # replaced, not merged
        assert response.headers["ETag"] != etag

        stored = next(iter(repo.models.values()))
        assert stored.process_config == {
            "anonymous-access": True,
            "deterministic": False,
            "poll-interval": 5.0,
            "result-storage": "remote",
        }
        event = repo.audit[-1]
        assert event.action == "model.card.update"
        assert event.details["changes"]["license"] == {
            "before": None,
            "after": "Apache-2.0",
        }


@pytest.mark.parametrize(
    "patch, status",
    [
        ({"purpose": {"fr": "but"}}, 400),  # not a platform language
        ({"purpose": {"de": "  "}}, 400),  # empty text
        ({"full_name": {"en": "Only English"}}, 400),  # default language missing
        ({"full_name": None}, 400),
        ({"license": "not a licence!"}, 400),
        ({"contributors": [{"name": "Ada", "role": "boss"}]}, 422),
        ({"test_cases": [{"name": "t", "input_url": "x"}]}, 422),
        ({"status": "published"}, 422),  # not a card field
    ],
)
def test_card_validation(patch, status):
    client, *_ = _setup()
    with client:
        model_id, etag = _draft(client)
        assert (
            _call(
                client, "PATCH", f"/models/{model_id}", "owner", etag, json=patch
            ).status_code
            == status
        )


def test_card_edit_permissions():
    client, *_ = _setup()
    with client:
        model_id, etag = _draft(client)
        patch = {"code_ref": "v1"}
        # editing needs the provider role (verifier: 403); another provider
        # does not even see the draft (404)
        assert (
            _call(
                client, "PATCH", f"/models/{model_id}", "verifier", etag, json=patch
            ).status_code
            == 403
        )
        assert (
            _call(
                client, "PATCH", f"/models/{model_id}", "other", etag, json=patch
            ).status_code
            == 404
        )
        assert (
            _call(
                client, "PATCH", f"/models/{model_id}", None, etag, json=patch
            ).status_code
            == 401
        )
        assert (
            _call(
                client, "PATCH", f"/models/{model_id}", "owner", json=patch
            ).status_code
            == 428
        )


def _set_status(repo, status, **extra):
    key, model = next(iter(repo.models.items()))
    repo.models[key] = model.model_copy(
        update={
            "status": status,
            "updated_at": datetime(2026, 10, 2, tzinfo=timezone.utc),
            **extra,
        }
    )
    return repo.models[key]


def test_published_card_edits_reload_snapshot_but_not_execution_settings():
    client, repo, _, providers, _ = _setup()
    with client:
        model_id, _ = _draft(client)
        _set_status(
            repo,
            "published",
            purpose={"de": "x"},
            license="MIT",
            verified_by="ver-1",
            verified_interface_sha256="a" * 64,
        )
        etag = _call(client, "GET", f"/models/{model_id}", "owner").headers["ETag"]

        blocked = _call(
            client,
            "PATCH",
            f"/models/{model_id}",
            "owner",
            etag,
            json={"process_config": {"anonymous-access": True}},
        )
        assert blocked.status_code == 409

        # a published card must stay complete
        assert (
            _call(
                client,
                "PATCH",
                f"/models/{model_id}",
                "owner",
                etag,
                json={"license": None},
            ).status_code
            == 400
        )

        reloads = providers.reloads
        ok = _call(
            client,
            "PATCH",
            f"/models/{model_id}",
            "owner",
            etag,
            json={"version_label": "2"},
        )
        assert ok.status_code == 200 and providers.reloads == reloads + 1


def test_deactivated_model_cannot_be_edited():
    client, repo, *_ = _setup()
    with client:
        model_id, _ = _draft(client)
        _set_status(repo, "deactivated", purpose={"de": "x"}, license="MIT")
        etag = _call(client, "GET", f"/models/{model_id}", "owner").headers["ETag"]
        assert (
            _call(
                client,
                "PATCH",
                f"/models/{model_id}",
                "owner",
                etag,
                json={"code_ref": "x"},
            ).status_code
            == 409
        )
