"""ToolCatalogService — projects the process catalog into agent-callable tools.

This is the core half of the MCP integration (reports/REF-F10-mcp-endpoint.md).
UMP knows nothing about the MCP protocol; it publishes a role-filtered view of
the processes it already federates, and the external MCP server
(https://github.com/Urban-Futures-Collective/ump-x-mcp) turns that into tools.

Hexagonal note: this service introduces no new port. Everything it needs is
already reachable through ``ProcessManager`` and ``AuthorizationService`` — both
core components — so it depends on them directly. Ports exist to keep
infrastructure out of the core, not to mediate between core collaborators.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.process import Process
from ump.core.models.tool import ToolCatalog, ToolDescriptor
from ump.core.services.authorization import AuthorizationService
from ump.core.settings import logger
from ump.core.utils.input_schema import ogc_inputs_to_json_schema


class ToolCatalogService:
    """Builds the per-caller tool catalog served at ``GET /mcp/tools``."""

    def __init__(
        self,
        process_manager: ProcessManager,
        authorization_service: AuthorizationService,
        process_id_validator: ProcessIdValidatorPort,
    ) -> None:
        self._processes = process_manager
        self._authz = authorization_service
        self._ids = process_id_validator

    async def build(
        self, auth: AuthContext, *, unrestricted: bool = False
    ) -> ToolCatalog:
        """Return the tools *auth* may see.

        ``unrestricted`` bypasses role filtering; the web adapter sets it when
        authentication is disabled globally, so that the documented meaning of
        ``UMP_AUTH_ENABLED=false`` ("bypass all auth checks") actually holds
        here. It is never derived from anything the caller sends.

        An empty catalog is a valid answer — an anonymous caller on a
        deployment without ``anonymous-access`` processes gets no tools, not an
        error.
        """
        allowed = await self._allowed_process_ids(auth, unrestricted=unrestricted)
        if not allowed:
            return ToolCatalog(tools=[])

        # ProcessSummary carries no `inputs` (get_all_processes downcasts and
        # caches summaries), and `inputs` is what becomes inputSchema — so each
        # allowed process is resolved to its full description here.
        results = await asyncio.gather(
            *(self._processes.get_process(pid) for pid in allowed),
            return_exceptions=True,
        )

        tools: List[ToolDescriptor] = []
        for process_id, result in zip(allowed, results):
            if isinstance(result, BaseException):
                # One unreachable provider must not empty the whole catalog.
                logger.warning(
                    f"[mcp] skipping tool '{process_id}': "
                    f"could not fetch process description: {result}"
                )
                continue
            descriptor = self._to_tool(process_id, result)
            if descriptor is not None:
                tools.append(descriptor)

        tools.sort(key=lambda t: t.tool)
        return ToolCatalog(tools=tools)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _allowed_process_ids(
        self, auth: AuthContext, *, unrestricted: bool
    ) -> List[str]:
        """Canonical ids of the processes visible to *auth*.

        The id list comes from ``get_all_processes`` so that provider exclusion,
        caching and per-provider fetch failures follow exactly the same rules as
        the OGC process list — the catalog is a projection of it, never a second
        source of truth.
        """
        try:
            process_list = await self._processes.get_all_processes()
        except Exception as exc:
            logger.error(f"[mcp] could not list processes for tool catalog: {exc}")
            return []

        return [
            summary.pid
            for summary in process_list.processes
            if unrestricted or self._authz.can_access_process(auth, summary.pid)
        ]

    def _to_tool(self, process_id: str, process: Process) -> Optional[ToolDescriptor]:
        provider, bare_id = self._split(process_id)
        if provider is None:
            logger.warning(
                f"[mcp] skipping tool '{process_id}': not a provider-prefixed id"
            )
            return None

        # ProcessInput.scheme is aliased to "schema"; dump by alias so the mapper
        # sees the wire shape the OGC description actually uses.
        inputs = (
            {
                name: definition.model_dump(by_alias=True, exclude_none=True)
                for name, definition in process.inputs.items()
            }
            if process.inputs
            else {}
        )

        return ToolDescriptor(
            tool=process_id,
            title=process.title or bare_id,
            description=process.description or "",
            inputSchema=ogc_inputs_to_json_schema(inputs),
            provider=provider,
            processId=bare_id,
        )

    def _split(self, process_id: str) -> Tuple[Optional[str], str]:
        try:
            provider, bare_id = self._ids.extract(process_id)
            return provider, bare_id
        except ValueError:
            return None, process_id
