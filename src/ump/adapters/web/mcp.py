"""MCP router — HTTP surface for the tool catalog.

Mounted at ``/mcp``, deliberately outside the versioned OGC router: the OGC
endpoints are standardised and are not touched by the MCP integration, and the
catalog contract versions on its own clock (``ToolCatalog.version``).

This module owns HTTP concerns only. The catalog itself, including all role
filtering, is built by ``ToolCatalogService`` in the core.
See reports/REF-F10-mcp-endpoint.md.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import APIRouter, Request

from ump.core.interfaces.auth import AuthContext
from ump.core.models.tool import ToolCatalog


def create_mcp_router(
    get_auth: Callable[[Request], Awaitable[AuthContext]],
    auth_enabled: Callable[[], bool],
) -> APIRouter:
    """Build the ``/mcp`` router.

    ``get_auth`` resolves the caller's context (the same dependency the OGC
    routes use). ``auth_enabled`` reports whether authentication is wired at
    all — the web adapter owns that question, exactly as ``_check_process_access``
    does for the execution route.
    """
    router = APIRouter(tags=["mcp"])

    @router.get(
        "/tools",
        response_model=ToolCatalog,
        response_model_exclude_none=True,
        summary="Role-filtered tool catalog for MCP servers",
    )
    async def list_tools(request: Request) -> ToolCatalog:
        """Per-caller tool catalog.

        Intentionally never answers 401: an anonymous caller receives the
        catalog filtered to processes marked ``anonymous-access: true``, which
        may legitimately be empty. The external MCP server supports anonymous
        operation, and a 401 here would break it. Execution remains guarded by
        the OGC execution route, which is the final authorization authority.
        """
        auth = await get_auth(request)
        return await request.app.state.tool_catalog.build(
            auth, unrestricted=not auth_enabled()
        )

    return router
