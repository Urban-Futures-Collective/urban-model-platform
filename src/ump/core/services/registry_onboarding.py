"""RegistryOnboardingService — register servers and their models (REF-F12 phase B,
"Provider onboarding and interface discovery").

- ``create_server`` / ``update_server``: a ``provider`` registers a model server
  (owner = caller) and edits it; platform admins may edit any server. The name
  prefixes process ids and roles and is immutable. Credentials are encrypted
  before they reach the repository and are never returned.
- ``discover``: lists the processes the server advertises, marking those that
  are already registered.
- ``create_models``: one ``draft`` model per selected process, pre-filled from
  the raw OGC description (fetched before any of UMP's process handlers, so the
  interface hash reflects the provider, not UMP). Titles/descriptions that the
  server returns differently per ``Accept-Language`` are stored per language.

Every user-supplied URL goes through ``SafeFetchPort``. Every write carries an
``AuditEvent``. Writes that can change the provider snapshot trigger a reload.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Sequence
from uuid import UUID

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.credential_cipher import (
    CredentialCipherPort,
    CredentialDecryptionError,
)
from ump.core.interfaces.model_registry import (
    ModelQuery,
    ModelRegistryPort,
    RegistryConflictError,
    StaleWriteError,
)
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.interfaces.safe_fetch import (
    RemoteFetchError,
    SafeFetchPort,
    UnsafeUrlError,
)
from ump.core.models.providers_config import (
    AuthConfig,
    NoAuthConfig,
    auth_config_from_secret,
)
from ump.core.models.registry import (
    SLUG_PATTERN,
    AppUser,
    ProviderServer,
    RegistryModel,
    interface_sha256,
    roles,
    slugify,
)
from ump.core.models.registry.audit import AuditEvent, diff
from ump.core.models.registry.card import ModelsCreate, ServerCreate, ServerPatch
from ump.core.models.registry.views import (
    CreatedModel,
    DiscoveredProcess,
    DiscoveryResult,
    ModelsCreated,
    ProviderServerView,
)
from ump.core.services.authorization import AuthorizationService
from ump.core.services.registry_common import check_if_match, owns_server, problem
from ump.core.services.registry_servers import server_view

logger = logging.getLogger(__name__)

# Server fields recorded in audit details; credentials only as a marker.
_AUDITED_SERVER_FIELDS = ("base_url", "auth_type", "ttw_job_done_seconds", "is_active")


def _with_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


class RegistryOnboardingService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        fetch: SafeFetchPort,
        remote_auth: RemoteAuthPort,
        cipher: CredentialCipherPort | None,
        authorization: AuthorizationService,
        providers: ProvidersPort,
        languages: Sequence[str],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not languages:
            raise ValueError("at least one registry language is required")
        self._registry = registry
        self._fetch = fetch
        self._remote_auth = remote_auth
        self._cipher = cipher
        self._authz = authorization
        self._providers = providers
        self._languages = list(languages)
        self._clock = clock

    # ------------------------------------------------------------------
    # Servers
    # ------------------------------------------------------------------

    async def create_server(
        self, auth: AuthContext, body: ServerCreate
    ) -> ProviderServerView:
        self._authz.require_platform_role(auth, roles.PROVIDER, roles.PLATFORM_ADMIN)
        if not SLUG_PATTERN.match(body.name):
            raise problem(
                400,
                "Bad Request",
                "name: lower-case letters, digits and single hyphens only; it "
                "prefixes process ids and roles and cannot be changed later",
            )
        base_url = self._safe_url(str(body.base_url))
        auth_type, credentials = self._encrypt(body.authentication)

        server = ProviderServer(
            name=body.name,
            base_url=base_url,
            owner_sub=auth.user_id,
            auth_type=auth_type,
            credentials=credentials,
            ttw_job_done_seconds=body.ttw_job_done_seconds,
        )
        await self._registry.upsert_user(AppUser(sub=auth.user_id))  # FK owner
        try:
            stored = await self._registry.create_provider_server(
                server,
                audit=AuditEvent.by(
                    auth,
                    "server.create",
                    details={
                        "server_id": str(server.id),
                        "name": server.name,
                        "base_url": base_url,
                        "auth_type": auth_type,
                    },
                ),
            )
        except RegistryConflictError:
            raise problem(409, "Conflict", f"Server name '{body.name}' is taken.")
        return server_view(stored)

    async def update_server(
        self,
        auth: AuthContext,
        server_id: UUID,
        patch: ServerPatch,
        if_match: str | None,
    ) -> ProviderServerView:
        server = await self._owned_server(auth, server_id)
        check_if_match(if_match, server.updated_at)

        changes: dict[str, Any] = {}
        sent = patch.model_fields_set
        if "base_url" in sent and patch.base_url is not None:
            changes["base_url"] = self._safe_url(str(patch.base_url))
        if "authentication" in sent:
            changes["auth_type"], changes["credentials"] = self._encrypt(
                patch.authentication
            )
        if "ttw_job_done_seconds" in sent and patch.ttw_job_done_seconds is not None:
            changes["ttw_job_done_seconds"] = patch.ttw_job_done_seconds
        if "is_active" in sent and patch.is_active is not None:
            changes["is_active"] = patch.is_active
        if not changes:
            return server_view(server)

        updated = server.model_copy(update=changes)
        details = diff(
            server.model_dump(include=set(_AUDITED_SERVER_FIELDS)),
            updated.model_dump(include=set(_AUDITED_SERVER_FIELDS)),
        )
        if "credentials" in changes:
            details["credentials"] = (
                "removed" if changes["credentials"] is None else "replaced"
            )
        details["server_id"] = str(server.id)
        stored = await self._store_server(
            updated, server, AuditEvent.by(auth, "server.update", details=details)
        )
        self._providers.load_providers()  # url, auth, ttw, active are in the snapshot
        return server_view(stored)

    # ------------------------------------------------------------------
    # Discovery and draft models
    # ------------------------------------------------------------------

    async def discover(self, auth: AuthContext, server_id: UUID) -> DiscoveryResult:
        server = await self._owned_server(auth, server_id)
        listing = await self._get(server, "processes")
        entries = listing.get("processes") if isinstance(listing, dict) else None
        if not isinstance(entries, list):
            raise problem(
                502, "Bad Gateway", "The server's /processes response has no list."
            )
        registered = {
            m.remote_process_id: m.id
            for m in await self._registry.list_models(
                ModelQuery(provider_server_id=server.id, limit=100_000)
            )
        }
        processes = [
            DiscoveredProcess(
                id=str(entry["id"]),
                title=entry.get("title"),
                description=entry.get("description"),
                version=entry.get("version"),
                model_id=registered.get(str(entry["id"])),
            )
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        ]
        await self._store_server(
            server.model_copy(update={"last_discovered_at": self._clock()}),
            server,
            AuditEvent.by(
                auth,
                "server.discover",
                details={"server_id": str(server.id), "processes": len(processes)},
            ),
        )
        return DiscoveryResult(server_id=server.id, processes=processes)

    async def create_models(
        self, auth: AuthContext, server_id: UUID, body: ModelsCreate
    ) -> ModelsCreated:
        server = await self._owned_server(auth, server_id)
        lang = body.default_lang or self._languages[0]
        if lang not in self._languages:
            raise problem(
                400, "Bad Request", f"default_lang must be one of {self._languages}"
            )

        existing: list[CreatedModel] = []
        drafts: list[RegistryModel] = []
        taken_slugs: set[str] = set()
        for pid in body.remote_process_ids:
            known = await self._registry.get_model(server.id, pid)
            if known is not None:
                existing.append(_created(known))
                continue
            drafts.append(await self._draft(auth, server, pid, lang, taken_slugs))

        if drafts:
            await self._registry.upsert_user(AppUser(sub=auth.user_id))  # FK owner
            try:
                await self._registry.create_models(
                    drafts,
                    audit=[
                        AuditEvent.by(
                            auth,
                            "model.create",
                            model_id=m.id,
                            details={
                                "server": server.name,
                                "remote_process_id": m.remote_process_id,
                                "slug": m.slug,
                                "status": m.status,
                            },
                        )
                        for m in drafts
                    ],
                )
            except RegistryConflictError:
                raise problem(
                    409,
                    "Conflict",
                    "A model was registered concurrently; retry the request.",
                )
        return ModelsCreated(created=[_created(m) for m in drafts], existing=existing)

    async def _draft(
        self,
        auth: AuthContext,
        server: ProviderServer,
        pid: str,
        lang: str,
        taken_slugs: set[str],
    ) -> RegistryModel:
        description = await self._get(server, f"processes/{pid}", lang=lang)
        if not isinstance(description, dict):
            raise problem(
                502, "Bad Gateway", f"Process '{pid}' description is not a JSON object."
            )
        texts = {lang: description}
        for other in self._languages:
            if other != lang:
                try:
                    texts[other] = await self._get(
                        server, f"processes/{pid}", lang=other
                    )
                except OGCProcessException:
                    continue  # an extra language is optional

        def per_language(key: str) -> dict[str, str]:
            base = description.get(key)
            out = {lang: base} if isinstance(base, str) and base.strip() else {}
            for other, doc in texts.items():
                value = doc.get(key) if isinstance(doc, dict) else None
                # only store a translation the server really sent
                if other != lang and isinstance(value, str) and value.strip():
                    if value != base:
                        out[other] = value
            return out

        full_name = per_language("title") or {lang: pid}
        keywords = description.get("keywords")
        sha = interface_sha256(description)
        return RegistryModel(
            slug=await self._free_slug(slugify(f"{server.name}-{pid}"), taken_slugs),
            provider_server_id=server.id,
            remote_process_id=pid,
            owner_sub=auth.user_id,
            default_lang=lang,
            full_name=full_name if lang in full_name else {lang: pid, **full_name},
            short_description=per_language("description"),
            keywords=(
                {lang: [str(k) for k in keywords if str(k).strip()]}
                if isinstance(keywords, list) and keywords
                else {}
            ),
            version_label=description.get("version"),
            status="draft",
            current_interface_sha256=sha,
            last_checked_at=self._clock(),
        )

    async def _free_slug(self, base: str, taken: set[str]) -> str:
        candidate, n = base, 1
        while candidate in taken or await self._registry.get_model_by_slug(candidate):
            n += 1
            candidate = f"{base}-{n}"
        taken.add(candidate)
        return candidate

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _owned_server(self, auth: AuthContext, server_id: UUID) -> ProviderServer:
        """The server, if the caller owns it (provider) or is a platform admin."""
        self._authz.require_platform_role(auth, roles.PROVIDER, roles.PLATFORM_ADMIN)
        server = await self._registry.get_provider_server_by_id(server_id)
        if server is None or not owns_server(auth, server):
            raise problem(404, "Not Found", f"Server '{server_id}' not found.")
        return server

    async def _store_server(
        self, updated: ProviderServer, loaded: ProviderServer, audit: AuditEvent
    ) -> ProviderServer:
        try:
            return await self._registry.update_provider_server(
                updated, expected_updated_at=loaded.updated_at, audit=audit
            )
        except StaleWriteError:
            raise problem(
                412,
                "Precondition Failed",
                "The server was changed concurrently; reload and retry.",
            )

    def _safe_url(self, url: str) -> str:
        url = _with_slash(url)
        try:
            self._fetch.check_url(url)
        except UnsafeUrlError as exc:
            raise problem(400, "Bad Request", f"base_url not allowed: {exc}")
        return url

    def _encrypt(self, auth: AuthConfig | None) -> tuple[str, bytes | None]:
        if auth is None or isinstance(auth, NoAuthConfig):
            return "NoAuth", None
        if self._cipher is None:
            raise problem(
                503,
                "Service Unavailable",
                "Credential storage is not configured (UMP_REGISTRY_CREDENTIALS_KEY).",
            )
        secret = {
            k: v.get_secret_value() if hasattr(v, "get_secret_value") else v
            for k, v in auth
            if k != "type"
        }
        return auth.type, self._cipher.encrypt(json.dumps(secret))

    def _outbound_auth(self, server: ProviderServer) -> AuthConfig:
        if server.auth_type == "NoAuth" or server.credentials is None:
            return NoAuthConfig()
        if self._cipher is None:
            raise problem(
                503,
                "Service Unavailable",
                "Credential storage is not configured (UMP_REGISTRY_CREDENTIALS_KEY).",
            )
        try:
            secret = json.loads(self._cipher.decrypt(server.credentials))
        except CredentialDecryptionError:
            raise problem(
                409,
                "Conflict",
                "Stored credentials cannot be decrypted; set them again.",
            )
        return auth_config_from_secret(server.auth_type, secret)

    async def _get(self, server: ProviderServer, path: str, lang: str | None = None):
        headers = dict(self._remote_auth.resolve(self._outbound_auth(server)).headers)
        if lang:
            headers["Accept-Language"] = lang
        url = server.base_url + path
        try:
            return await self._fetch.get_json(url, headers=headers)
        except UnsafeUrlError as exc:
            raise problem(400, "Bad Request", f"{url} not allowed: {exc}")
        except RemoteFetchError as exc:
            raise problem(502, "Bad Gateway", f"{url}: {exc}")


def _created(model: RegistryModel) -> CreatedModel:
    return CreatedModel(
        id=model.id,
        slug=model.slug,
        remote_process_id=model.remote_process_id,
        status=model.status,
    )
