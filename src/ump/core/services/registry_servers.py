"""RegistryServerService — read access to provider servers (REF-F12 phase A).

Providers see their own servers, platform admins all of them. A server outside
the caller's scope is reported as 404. Credentials are never returned; views
only say whether some are stored.
"""

from __future__ import annotations

from uuid import UUID

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.model_registry import ModelRegistryPort
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.registry import ProviderServer, roles
from ump.core.models.registry.views import ProviderServerList, ProviderServerView
from ump.core.services.authorization import AuthorizationService


def server_view(server: ProviderServer) -> ProviderServerView:
    return ProviderServerView(
        id=server.id,
        name=server.name,
        base_url=server.base_url,
        owner_sub=server.owner_sub,
        auth_type=server.auth_type,
        credentials_set=server.credentials is not None,
        ttw_job_done_seconds=server.ttw_job_done_seconds,
        is_active=server.is_active,
        last_discovered_at=server.last_discovered_at,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


class RegistryServerService:
    def __init__(
        self, registry: ModelRegistryPort, authorization: AuthorizationService
    ) -> None:
        self._registry = registry
        self._authz = authorization

    async def list_servers(self, auth: AuthContext) -> ProviderServerList:
        self._authz.require_platform_role(auth, roles.PROVIDER, roles.PLATFORM_ADMIN)
        owner = None if roles.PLATFORM_ADMIN in auth.roles else auth.user_id
        servers = await self._registry.list_provider_servers(owner_sub=owner)
        return ProviderServerList(servers=[server_view(s) for s in servers])

    async def get_server(
        self, auth: AuthContext, server_id: UUID
    ) -> ProviderServerView:
        self._authz.require_platform_role(auth, roles.PROVIDER, roles.PLATFORM_ADMIN)
        server = await self._registry.get_provider_server_by_id(server_id)
        if server is None or not (
            roles.PLATFORM_ADMIN in auth.roles or server.owner_sub == auth.user_id
        ):
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Not Found",
                    status=404,
                    detail=f"Server '{server_id}' not found.",
                )
            )
        return server_view(server)
