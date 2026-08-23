import asyncio
import json

from apiflask import APIBlueprint
from flask import Response

from ump.api.mcp import load_mcp_tools

mcp = APIBlueprint("mcp", __name__)


@mcp.route("/tools", methods=["GET"])
def tools():
    """Per-user, role-filtered MCP tool catalog.

    An external MCP server (see ARCHITECTURE.md / mcp-integration-strategy.md)
    is the intended caller: it forwards the same user JWT it validated,
    translates each entry into an MCP tool, and forwards tool invocations to
    POST /processes/{id}/execution using that same JWT.
    """
    result = asyncio.run(load_mcp_tools())
    return Response(json.dumps(result), mimetype="application/json")
