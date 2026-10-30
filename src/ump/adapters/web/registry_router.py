"""Registry router — HTTP surface of the model registry (REF-F12).

Mounted at ``/registry/v{major}`` (``UMP_REGISTRY_API_VERSIONS``), outside the
versioned OGC router, like the MCP router: the OGC API Processes endpoints are
standardised and are not changed by the registry. The registry reaches them
only through the ProvidersPort snapshot.

This module owns HTTP concerns only — language negotiation, query parameters,
headers (ETag / If-Match / Location). Visibility, role checks, validation and
auditing live in the core services. Writes need an authenticated caller, so
with ``UMP_AUTH_ENABLED=false`` (every caller anonymous) they are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional, Sequence
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from ump.core.interfaces.audit_log import AuditLogPort
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.credential_cipher import CredentialCipherPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.model_registry import ModelRegistryPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.interfaces.safe_fetch import SafeFetchPort
from ump.core.models.registry.card import (
    AccessGrantCreate,
    AccessRequestCreate,
    AccessTransitionRequest,
    ModelCardPatch,
    ModelsCreate,
    OwnerTransfer,
    ReverifyRequest,
    ServerCreate,
    ServerPatch,
    TransitionRequest,
)
from ump.core.models.registry.etag import etag_for
from ump.core.models.registry.localized import parse_accept_language
from ump.core.models.registry.views import (
    AccessRequestPage,
    AccessRequestView,
    AuditPage,
    DiscoveryResult,
    InterfaceView,
    MeView,
    ModelCardView,
    ModelPage,
    ModelsCreated,
    ProviderServerList,
    ProviderServerView,
    RediscoveryResult,
    RoleVocabulary,
)
from ump.core.services.authorization import AuthorizationService
from ump.core.services.interface_drift import InterfaceDriftService
from ump.core.services.registry_access import RegistryAccessService
from ump.core.services.registry_accounts import RegistryAccountService
from ump.core.services.registry_audit import RegistryAuditService
from ump.core.services.registry_cards import RegistryCardService
from ump.core.services.registry_catalog import RegistryCatalogService
from ump.core.services.registry_onboarding import RegistryOnboardingService
from ump.core.services.registry_servers import RegistryServerService
from ump.core.services.registry_verification import RegistryVerificationService


@dataclass
class RegistryServices:
    """Core services behind the registry router, built by the composition root."""

    catalog: RegistryCatalogService
    servers: RegistryServerService
    accounts: RegistryAccountService
    onboarding: RegistryOnboardingService
    cards: RegistryCardService
    verification: RegistryVerificationService
    drift: InterfaceDriftService
    audit: RegistryAuditService
    access: RegistryAccessService

    @classmethod
    def create(
        cls,
        registry: ModelRegistryPort,
        authorization: AuthorizationService,
        providers: ProvidersPort,
        fetch: SafeFetchPort,
        remote_auth: RemoteAuthPort,
        cipher: CredentialCipherPort | None,
        languages: Sequence[str],
        public_catalogue: bool,
        jobs: JobRepositoryPort,
        audit_log: AuditLogPort,
    ) -> "RegistryServices":
        """Assemble the core services from injected ports (no adapters built here)."""
        catalog = RegistryCatalogService(registry, authorization, public_catalogue)
        return cls(
            catalog=catalog,
            servers=RegistryServerService(registry, authorization),
            accounts=RegistryAccountService(registry),
            onboarding=RegistryOnboardingService(
                registry,
                fetch=fetch,
                remote_auth=remote_auth,
                cipher=cipher,
                authorization=authorization,
                providers=providers,
                languages=languages,
            ),
            cards=RegistryCardService(
                registry,
                catalog=catalog,
                authorization=authorization,
                providers=providers,
                languages=languages,
            ),
            verification=RegistryVerificationService(
                registry,
                jobs=jobs,
                catalog=catalog,
                authorization=authorization,
                providers=providers,
                fetch=fetch,
                remote_auth=remote_auth,
                cipher=cipher,
            ),
            drift=InterfaceDriftService(
                registry,
                catalog=catalog,
                authorization=authorization,
                fetch=fetch,
                remote_auth=remote_auth,
                cipher=cipher,
            ),
            audit=RegistryAuditService(audit_log, authorization),
            access=RegistryAccessService(registry, catalog, authorization),
        )


def _languages(request: Request, lang: Optional[str]) -> list[str]:
    """``?lang=`` wins over Accept-Language; ``*`` asks for every language."""
    if lang:
        return [lang]
    return parse_accept_language(request.headers.get("accept-language"))


def _access_headers(
    request: Request, response: Response, view: AccessRequestView
) -> None:
    response.headers["Location"] = str(
        request.url_for("get_access_request", request_id=view.id).path
    )
    response.headers["ETag"] = etag_for(view.updated_at)


def create_registry_router(
    get_auth: Callable[[Request], Awaitable[AuthContext]],
    services: RegistryServices,
) -> APIRouter:
    router = APIRouter(tags=["registry"])

    async def caller(request: Request) -> AuthContext:
        auth = await get_auth(request)
        await services.accounts.touch(auth)  # app_user just-in-time, throttled
        return auth

    @router.get("/me", response_model=MeView, summary="The caller's account")
    async def me(request: Request) -> MeView:
        return await services.accounts.me(await get_auth(request))

    @router.get(
        "/models",
        response_model=ModelPage,
        summary="Model cards visible to the caller",
    )
    async def list_models(
        request: Request,
        lang: Optional[str] = Query(None, description="language, or * for all"),
        status: Optional[str] = None,
        provider: Optional[str] = Query(None, description="provider server name"),
        mine: bool = False,
        cursor: Optional[str] = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> ModelPage:
        return await services.catalog.list_models(
            await caller(request),
            languages=_languages(request, lang),
            status=status,
            provider=provider,
            mine=mine,
            cursor=cursor,
            limit=limit,
        )

    @router.get(
        "/models/{model_id}",
        response_model=ModelCardView,
        summary="One model card",
    )
    async def get_model(
        model_id: UUID,
        request: Request,
        response: Response,
        lang: Optional[str] = Query(None, description="language, or * for all"),
    ) -> ModelCardView:
        view = await services.catalog.get_model(
            await caller(request), model_id, languages=_languages(request, lang)
        )
        if view.lang != "*":
            response.headers["Content-Language"] = view.lang
        response.headers["Vary"] = "Accept-Language, Authorization"
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.patch(
        "/models/{model_id}",
        response_model=ModelCardView,
        summary="Edit a model card (owner; If-Match required)",
    )
    async def patch_model(
        model_id: UUID,
        body: ModelCardPatch,
        request: Request,
        response: Response,
        if_match: Optional[str] = Header(None),
    ) -> ModelCardView:
        view = await services.cards.update_card(
            await caller(request), model_id, body, if_match
        )
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.post(
        "/models/{model_id}/transitions",
        response_model=ModelCardView,
        summary="Move a model through the review workflow",
    )
    async def transition(
        model_id: UUID,
        body: TransitionRequest,
        request: Request,
        response: Response,
        if_match: Optional[str] = Header(None),
    ) -> ModelCardView:
        view = await services.verification.transition(
            await caller(request), model_id, body, if_match
        )
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.post(
        "/models/{model_id}/reverify",
        response_model=ModelCardView,
        summary="Re-verify a verified/published model against its current interface",
    )
    async def reverify(
        model_id: UUID,
        body: ReverifyRequest,
        request: Request,
        response: Response,
        if_match: Optional[str] = Header(None),
    ) -> ModelCardView:
        view = await services.verification.reverify(
            await caller(request), model_id, body, if_match
        )
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.get(
        "/models/{model_id}/interface",
        response_model=InterfaceView,
        summary="Interface drift state and live comparison (owner, verifier)",
    )
    async def model_interface(model_id: UUID, request: Request) -> InterfaceView:
        return await services.drift.interface(await caller(request), model_id)

    @router.patch(
        "/models/{model_id}/owner",
        response_model=ModelCardView,
        summary="Transfer a model to another account (platform admin)",
    )
    async def transfer_owner(
        model_id: UUID,
        body: OwnerTransfer,
        request: Request,
        response: Response,
        if_match: Optional[str] = Header(None),
    ) -> ModelCardView:
        view = await services.verification.transfer_owner(
            await caller(request), model_id, body, if_match
        )
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.post(
        "/models/{model_id}/access-requests",
        response_model=AccessRequestView,
        status_code=201,
        summary="Request access to a published model",
    )
    async def request_access(
        model_id: UUID,
        body: AccessRequestCreate,
        request: Request,
        response: Response,
        lang: Optional[str] = Query(None),
    ) -> AccessRequestView:
        view = await services.access.request(
            await caller(request), model_id, body, _languages(request, lang)
        )
        _access_headers(request, response, view)
        return view

    @router.post(
        "/models/{model_id}/access-grants",
        response_model=AccessRequestView,
        status_code=201,
        summary="Record a grant made in Keycloak without a request (access admin)",
    )
    async def grant_access(
        model_id: UUID,
        body: AccessGrantCreate,
        request: Request,
        response: Response,
        lang: Optional[str] = Query(None),
    ) -> AccessRequestView:
        view = await services.access.grant_directly(
            await caller(request), model_id, body, _languages(request, lang)
        )
        _access_headers(request, response, view)
        return view

    @router.get(
        "/access-requests",
        response_model=AccessRequestPage,
        summary="Access requests: all for access admins, else own and own models'",
    )
    async def list_access_requests(
        request: Request,
        status: Optional[str] = None,
        model_id: Optional[UUID] = None,
        user: Optional[str] = Query(None, description="requesting user's sub"),
        cursor: Optional[str] = None,
        limit: int = Query(50, ge=1, le=200),
        lang: Optional[str] = Query(None),
    ) -> AccessRequestPage:
        return await services.access.list_requests(
            await caller(request),
            status=status,
            model_id=model_id,
            user=user,
            cursor=cursor,
            limit=limit,
            languages=_languages(request, lang),
        )

    @router.get(
        "/access-requests/{request_id}",
        response_model=AccessRequestView,
        summary="One access request",
    )
    async def get_access_request(
        request_id: UUID,
        request: Request,
        response: Response,
        lang: Optional[str] = Query(None),
    ) -> AccessRequestView:
        view = await services.access.get_request(
            await caller(request), request_id, _languages(request, lang)
        )
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.post(
        "/access-requests/{request_id}/transitions",
        response_model=AccessRequestView,
        summary="Withdraw, grant, deny or revoke an access request",
    )
    async def transition_access_request(
        request_id: UUID,
        body: AccessTransitionRequest,
        request: Request,
        response: Response,
        if_match: Optional[str] = Header(None),
        lang: Optional[str] = Query(None),
    ) -> AccessRequestView:
        view = await services.access.transition(
            await caller(request),
            request_id,
            body,
            if_match,
            _languages(request, lang),
        )
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.get(
        "/servers",
        response_model=ProviderServerList,
        summary="Provider servers: own ones, or all for platform admins",
    )
    async def list_servers(request: Request) -> ProviderServerList:
        return await services.servers.list_servers(await caller(request))

    @router.get(
        "/servers/{server_id}",
        response_model=ProviderServerView,
        summary="One provider server",
    )
    async def get_server(
        server_id: UUID, request: Request, response: Response
    ) -> ProviderServerView:
        view = await services.servers.get_server(await caller(request), server_id)
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.post(
        "/servers",
        response_model=ProviderServerView,
        status_code=201,
        summary="Register a provider server (provider)",
    )
    async def create_server(
        body: ServerCreate, request: Request, response: Response
    ) -> ProviderServerView:
        view = await services.onboarding.create_server(await caller(request), body)
        response.headers["Location"] = str(
            request.url_for("get_server", server_id=view.id).path
        )
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.patch(
        "/servers/{server_id}",
        response_model=ProviderServerView,
        summary="Edit a provider server (owner or platform admin; If-Match required)",
    )
    async def patch_server(
        server_id: UUID,
        body: ServerPatch,
        request: Request,
        response: Response,
        if_match: Optional[str] = Header(None),
    ) -> ProviderServerView:
        view = await services.onboarding.update_server(
            await caller(request), server_id, body, if_match
        )
        response.headers["ETag"] = etag_for(view.updated_at)
        return view

    @router.post(
        "/servers/{server_id}/discover",
        response_model=DiscoveryResult,
        summary="List the processes the server advertises",
    )
    async def discover(server_id: UUID, request: Request) -> DiscoveryResult:
        return await services.onboarding.discover(await caller(request), server_id)

    @router.post(
        "/servers/{server_id}/rediscover",
        response_model=RediscoveryResult,
        summary="Check the server's models for interface drift now",
    )
    async def rediscover(server_id: UUID, request: Request) -> RediscoveryResult:
        return await services.drift.rediscover(await caller(request), server_id)

    @router.post(
        "/servers/{server_id}/models",
        response_model=ModelsCreated,
        status_code=201,
        summary="Create draft models for selected processes",
    )
    async def create_models(
        server_id: UUID, body: ModelsCreate, request: Request
    ) -> ModelsCreated:
        return await services.onboarding.create_models(
            await caller(request), server_id, body
        )

    @router.get(
        "/roles/vocabulary",
        response_model=RoleVocabulary,
        summary="Model-access role names of every published model",
    )
    async def role_vocabulary(request: Request) -> RoleVocabulary:
        return await services.catalog.role_vocabulary(await caller(request))

    @router.get(
        "/audit",
        response_model=AuditPage,
        summary="Audit log, newest first (verifier, access-admin, platform-admin)",
    )
    async def audit(
        request: Request,
        model_id: Optional[UUID] = None,
        actor: Optional[str] = Query(None, description="actor sub"),
        action: Optional[str] = None,
        job_id: Optional[str] = None,
        since: Optional[datetime] = Query(None, description="inclusive"),
        until: Optional[datetime] = Query(None, description="exclusive"),
        cursor: Optional[str] = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> AuditPage:
        return await services.audit.list_events(
            await caller(request),
            model_id=model_id,
            actor=actor,
            action=action,
            job_id=job_id,
            since=since,
            until=until,
            cursor=cursor,
            limit=limit,
        )

    return router
