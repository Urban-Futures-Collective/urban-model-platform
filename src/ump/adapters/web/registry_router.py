"""Registry router — HTTP surface of the model registry (REF-F12).

Mounted at ``/registry/v{major}`` (``UMP_REGISTRY_API_VERSIONS``), outside the
versioned OGC router, like the MCP router: the OGC API Processes endpoints are
standardised and are not changed by the registry. The registry reaches them
only through the ProvidersPort snapshot.

This module owns HTTP concerns only — language negotiation, query parameters,
headers. Visibility and role checks live in the core services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from ump.core.interfaces.auth import AuthContext
from ump.core.models.registry.localized import parse_accept_language
from ump.core.models.registry.views import (
    MeView,
    ModelCardView,
    ModelPage,
    ProviderServerList,
    ProviderServerView,
    RoleVocabulary,
)
from ump.core.services.registry_accounts import RegistryAccountService
from ump.core.services.registry_catalog import RegistryCatalogService
from ump.core.services.registry_servers import RegistryServerService


@dataclass
class RegistryServices:
    """Core services behind the registry router, built by the composition root."""

    catalog: RegistryCatalogService
    servers: RegistryServerService
    accounts: RegistryAccountService


def _languages(request: Request, lang: Optional[str]) -> list[str]:
    """``?lang=`` wins over Accept-Language; ``*`` asks for every language."""
    if lang:
        return [lang]
    return parse_accept_language(request.headers.get("accept-language"))


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
    async def get_server(server_id: UUID, request: Request) -> ProviderServerView:
        return await services.servers.get_server(await caller(request), server_id)

    @router.get(
        "/roles/vocabulary",
        response_model=RoleVocabulary,
        summary="Model-access role names of every published model",
    )
    async def role_vocabulary(request: Request) -> RoleVocabulary:
        return await services.catalog.role_vocabulary(await caller(request))

    return router
