"""Tool catalog models — the contract published at ``GET /mcp/tools``.

An external MCP server (https://github.com/Urban-Futures-Collective/ump-x-mcp)
translates each ``ToolDescriptor`` into one MCP tool. UMP itself knows nothing
about the MCP protocol: it only publishes a role-filtered, agent-shaped view of
the process catalog. See ``reports/REF-F10-mcp-endpoint.md``.

Field names follow mcp-integration-strategy.md §4 and are a published contract —
changing them breaks the MCP server. ``ToolCatalog.version`` tracks this contract
and is intentionally independent of ``UMP_SUPPORTED_API_VERSIONS``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel

#: Version of the tool-catalog contract itself (not the OGC API version).
TOOL_CATALOG_VERSION = "1.0"


class ToolDescriptor(BaseModel):
    """One UMP process, described as an agent-callable tool."""

    tool: str
    """Canonical UMP process id, e.g. ``mock:hello-world``. Also the id used for
    ``POST /processes/{tool}/execution``."""

    title: str
    description: str = ""
    inputSchema: Dict[str, Any]
    """JSON Schema derived from the process' OGC ``inputs`` description."""

    provider: str
    processId: str
    """Bare process id as configured for the provider (no provider prefix)."""


class ToolCatalog(BaseModel):
    """Role-filtered set of tools visible to one caller."""

    version: str = TOOL_CATALOG_VERSION
    tools: List[ToolDescriptor]
