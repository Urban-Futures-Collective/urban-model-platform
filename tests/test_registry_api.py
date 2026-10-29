"""Registry API phase A (/registry/v1) over HTTP with in-memory adapters.

Covers visibility (D10, owner/verifier/admin scopes), language resolution,
pagination, servers without credential leaks, the role vocabulary and /me.
The app is wired the way asgi.py does it, with faked ports.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

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

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
SHA = "a" * 64
SEED = AuditEvent(actor_sub=None, action="test.seed")

CALLERS: Dict[str, AuthContext] = {
    "viewer": AuthContext("v-1", ["viewer"], True, email="v@x", name="Vera"),
    "owner": AuthContext("owner-1", ["provider"], True),
    "other-provider": AuthContext("owner-2", ["provider"], True),
    "verifier": AuthContext("ver-1", ["verifier"], True),
    "admin": AuthContext("adm-1", ["platform-admin"], True),
    "access-admin": AuthContext("acc-1", ["access-admin"], True),
    "nobody": AuthContext("n-1", [], True),
}
ANON = AuthContext(None, [], False)


class TokenAuth(AuthPort):
    async def verify(self, token):
        return CALLERS[token] if token else ANON


class NoProviders(ProvidersPort):
    def load_providers(self): ...

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
        raise AssertionError("registry phase A makes no outbound calls")

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
        raise AssertionError("phase A tests make no outbound calls")


def _model(server: ProviderServer, pid: str, status: str, owner="owner-1", **kw):
    published = status in ("published", "deactivated")
    return RegistryModel(
        slug=f"{server.name}-{pid}",
        provider_server_id=server.id,
        remote_process_id=pid,
        owner_sub=owner,
        full_name={"de": f"Modell {pid}", "en": f"Model {pid}"},
        purpose={"en": "Purpose"},
        license="MIT",
        status=status,
        status_note="looks fine" if status != "draft" else None,
        published_at=T0 if published else None,
        verified_by="ver-1" if status in ("verified", "published") else None,
        verified_interface_sha256=SHA if status in ("verified", "published") else None,
        **kw,
    )


async def _seed(repo: InMemoryRegistryRepository) -> Dict[str, Any]:
    await repo.upsert_user(AppUser(sub="owner-1"))
    srv = await repo.create_provider_server(
        ProviderServer(
            name="srv",
            base_url="http://srv.local/",
            owner_sub="owner-1",
            auth_type="BasicAuth",
            credentials=b"ciphertext-never-returned",
        ),
        audit=SEED,
    )
    other = await repo.create_provider_server(
        ProviderServer(name="other", base_url="http://o.local/", owner_sub="owner-2"),
        audit=SEED,
    )
    models = {
        "pub": _model(
            srv,
            "pub",
            "published",
            process_config={"anonymous-access": True},
            keywords={"de": ["Rad"], "en": ["bike"]},
            applications=[{"title": {"de": "Hamburg", "en": "Hamburg"}, "year": 2025}],
            io_notes={"x": {"de": "Meter", "en": "metres"}},
        ),
        "draft": _model(srv, "draft", "draft"),
        "review": _model(srv, "review", "in_review"),
        "gone": _model(srv, "gone", "deactivated"),
        "foreign-draft": _model(other, "fd", "draft", owner="owner-2"),
        "foreign-pub": _model(other, "fp", "published", owner="owner-2"),
    }
    await repo.create_models(list(models.values()), audit=[SEED] * len(models))
    return {"srv": srv, "other": other, **models}


def _client(public: bool = False):
    import asyncio

    repo = InMemoryRegistryRepository(clock=lambda: T0)
    seeded = asyncio.run(_seed(repo))
    providers = NoProviders()
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
        job_repo=InMemoryJobRepository("scratch/test_registry_api"),
        process_id_validator=validator,
        auth_port=TokenAuth(),
        authorization_service=authz,
        registry=RegistryServices.create(
            repo,
            authorization=authz,
            providers=providers,
            fetch=NoFetch(),
            remote_auth=RemoteAuthAdapter(),
            cipher=None,
            languages=["en", "de"],
            public_catalogue=public,
            jobs=InMemoryJobRepository("scratch/test_registry_jobs"),
            audit_log=InMemoryAuditLog(),
        ),
    )
    return TestClient(app), repo, seeded


def _get(client, path, who=None, **kw):
    headers = kw.pop("headers", {})
    if who:
        headers["Authorization"] = f"Bearer {who}"
    return client.get(f"/registry/v1{path}", headers=headers, **kw)


def _slugs(response) -> List[str]:
    assert response.status_code == 200, response.text
    return [m["slug"] for m in response.json()["models"]]


# --- mounting ---------------------------------------------------------------


def test_mounted_under_its_own_prefix_not_under_ogc():
    client, _, _ = _client(public=True)
    with client:
        assert client.get("/registry/v1/models").status_code == 200
        assert client.get("/v1.0/registry/models").status_code == 404
        assert client.get("/registry/models").status_code == 404


# --- catalogue visibility -----------------------------------------------------


def test_non_public_catalogue_needs_auth_and_a_catalogue_role():
    client, _, _ = _client(public=False)
    with client:
        assert _get(client, "/models").status_code == 401
        assert _slugs(_get(client, "/models", "viewer")) == [
            "other-fp",
            "srv-pub",
        ]
        # authenticated without a catalogue role: only own models (none)
        assert _slugs(_get(client, "/models", "nobody")) == []


def test_public_catalogue_shows_published_to_anonymous():
    client, _, _ = _client(public=True)
    with client:
        assert _slugs(_get(client, "/models")) == ["other-fp", "srv-pub"]


@pytest.mark.parametrize(
    "who, expected",
    [
        # owner: published of others + all own (any status)
        ("owner", ["other-fp", "srv-draft", "srv-gone", "srv-pub", "srv-review"]),
        # verifier: everything but drafts
        ("verifier", ["other-fp", "srv-gone", "srv-pub", "srv-review"]),
        (
            "admin",
            ["other-fd", "other-fp", "srv-draft", "srv-gone", "srv-pub", "srv-review"],
        ),
    ],
)
def test_scopes_by_role_and_ownership(who, expected):
    client, _, _ = _client(public=True)
    with client:
        assert _slugs(_get(client, "/models", who)) == expected


def test_filters_status_mine_provider():
    client, _, _ = _client(public=True)
    with client:
        assert _slugs(_get(client, "/models", "owner", params={"status": "draft"})) == [
            "srv-draft"
        ]
        # a status filter never widens visibility
        assert (
            _slugs(_get(client, "/models", "viewer", params={"status": "draft"})) == []
        )
        assert _slugs(_get(client, "/models", "owner", params={"mine": True})) == [
            "srv-draft",
            "srv-gone",
            "srv-pub",
            "srv-review",
        ]
        assert _slugs(_get(client, "/models", params={"provider": "other"})) == [
            "other-fp"
        ]
        assert _slugs(_get(client, "/models", params={"provider": "nope"})) == []
        assert _get(client, "/models", params={"mine": True}).status_code == 401
        assert _get(client, "/models", params={"status": "bogus"}).status_code == 400


def test_pagination_with_cursor():
    client, _, _ = _client(public=True)
    with client:
        first = _get(client, "/models", "admin", params={"limit": 4}).json()
        assert len(first["models"]) == 4 and first["next_cursor"]
        rest = _get(
            client,
            "/models",
            "admin",
            params={"limit": 4, "cursor": first["next_cursor"]},
        ).json()
        assert [m["slug"] for m in rest["models"]] == ["srv-pub", "srv-review"]
        assert rest["next_cursor"] is None
        assert _get(client, "/models", params={"cursor": "%%%"}).status_code == 400


def test_invisible_model_is_404_not_403():
    client, _, seeded = _client(public=True)
    with client:
        draft = seeded["draft"].id
        assert _get(client, f"/models/{draft}", "viewer").status_code == 404
        assert _get(client, f"/models/{draft}", "owner").status_code == 200
        assert _get(client, f"/models/{uuid4()}", "admin").status_code == 404


# --- model card content -------------------------------------------------------


def test_language_resolution_and_headers():
    client, _, seeded = _client(public=True)
    pub = seeded["pub"].id
    with client:
        de = _get(client, f"/models/{pub}", headers={"Accept-Language": "de;q=0.9, fr"})
        assert de.headers["Content-Language"] == "de"
        body = de.json()
        assert body["lang"] == "de" and body["languages"] == ["de", "en"]
        assert body["full_name"] == "Modell pub"
        assert body["purpose"] == "Purpose"  # no German text: default language
        assert body["keywords"] == ["Rad"]
        assert body["applications"] == [{"title": "Hamburg", "year": 2025}]
        assert body["io_notes"] == {"x": "Meter"}
        assert body["process_id"] == "srv:pub"

        en = _get(client, f"/models/{pub}", params={"lang": "fr"}).json()
        assert en["lang"] == "en" and en["full_name"] == "Model pub"

        whole = _get(client, f"/models/{pub}", params={"lang": "*"})
        assert "Content-Language" not in whole.headers
        assert whole.json()["full_name"] == {"de": "Modell pub", "en": "Model pub"}


def test_review_details_only_for_owner_verifier_admin():
    client, _, seeded = _client(public=True)
    pub = seeded["pub"].id
    with client:
        assert _get(client, f"/models/{pub}").json()["review"] is None
        assert _get(client, f"/models/{pub}", "viewer").json()["review"] is None
        for who in ("owner", "verifier", "admin"):
            review = _get(client, f"/models/{pub}", who).json()["review"]
            assert review["status_note"] == "looks fine"


# --- servers ------------------------------------------------------------------


def test_servers_scoped_and_credentials_never_returned():
    client, _, seeded = _client(public=True)
    with client:
        assert _get(client, "/servers").status_code == 401
        assert _get(client, "/servers", "viewer").status_code == 403

        own = _get(client, "/servers", "owner")
        assert [s["name"] for s in own.json()["servers"]] == ["srv"]
        assert own.json()["servers"][0]["credentials_set"] is True
        assert "ciphertext" not in own.text and 'credentials"' not in own.text

        everything = _get(client, "/servers", "admin").json()["servers"]
        assert [s["name"] for s in everything] == ["other", "srv"]

        other_id = seeded["other"].id
        assert _get(client, f"/servers/{other_id}", "owner").status_code == 404
        assert _get(client, f"/servers/{other_id}", "admin").status_code == 200


# --- vocabulary and identity ----------------------------------------------------


def test_role_vocabulary():
    client, _, _ = _client(public=True)
    with client:
        assert _get(client, "/roles/vocabulary", "viewer").status_code == 403
        for who in ("access-admin", "verifier"):
            body = _get(client, "/roles/vocabulary", who).json()
            # ever-published models, deactivated included: roles are never deleted
            assert body["roles"] == [
                "other",
                "other:fp",
                "srv",
                "srv:gone",
                "srv:pub",
            ]


def test_me_upserts_app_user_from_token():
    client, repo, _ = _client(public=True)
    with client:
        assert _get(client, "/me").status_code == 401
        body = _get(client, "/me", "viewer").json()
        assert body["sub"] == "v-1" and body["display_name"] == "Vera"
        assert body["platform_roles"] == ["viewer"]
        assert repo.users["v-1"].email == "v@x"


def test_owner_display_name_and_anonymous_flag():
    """Issue #13: a readable owner for every caller, and the anonymous flag."""
    import asyncio

    client, repo, seeded = _client(public=True)
    asyncio.run(repo.upsert_user(AppUser(sub="owner-1", display_name="Olga Owner")))
    with client:
        # anonymous caller on a public catalogue: names are visible to everyone
        listed = {m["slug"]: m for m in _get(client, "/models").json()["models"]}
        assert listed["srv-pub"]["owner_display_name"] == "Olga Owner"
        assert listed["srv-pub"]["anonymous_access"] is True
        # owner-2 never used the registry: no app_user row, no name
        assert listed["other-fp"]["owner_display_name"] is None
        assert listed["other-fp"]["anonymous_access"] is False

        one = _get(client, f"/models/{seeded['pub'].id}").json()
        assert one["owner_display_name"] == "Olga Owner"
        assert one["anonymous_access"] is True
        assert one["owner_sub"] == "owner-1"  # still there for links and checks


def test_any_registry_request_records_the_caller():
    client, repo, _ = _client(public=True)
    with client:
        _get(client, "/models", "verifier")
        assert "ver-1" in repo.users
        _get(client, "/models")  # anonymous callers are not recorded
        assert set(repo.users) == {"owner-1", "ver-1"}
