import asyncio
from logging import getLogger

import aiohttp
from aiohttp import ClientSession, ClientTimeout
from flask import g

from ump.api.models.providers_config import ProviderConfig
from ump.api.processes import has_user_access_rights
from ump.api.providers import get_providers
from ump.api.remote_auth import get_auth_strategy
from ump.config import app_settings
from ump.utils import fetch_json

logger = getLogger(__name__)

process_describe_timeout = ClientTimeout(
    total=5,  # remote server needs to answer in time, we may fetch many processes at once
    connect=2,
    sock_connect=2,
    sock_read=5,
)


async def load_mcp_tools() -> dict:
    """
    Builds the MCP tool catalog for the current user, filtered to the processes
    they are allowed to see (same role model as GET /processes), per the
    discovery contract in mcp-integration-strategy.md section 4.

    This is UMP's side of Phase 1 of the MCP roadmap: a dedicated, per-user
    filtered discovery endpoint that a separate MCP server translates into
    MCP tools. UMP remains the source of truth for the process catalog and
    for authorization; it does not know about MCP tool-calling itself.
    """
    auth = g.get("auth_token", {}) or {}

    realm_roles: list = auth.get("realm_access", {}).get("roles", [])
    client_roles: list = (
        auth.get("resource_access", {})
        .get(app_settings.UMP_KEYCLOAK_CLIENT_ID, {})
        .get("roles", [])
    )

    allowed_processes = [
        (provider_name, process_id)
        for provider_name, provider_config in get_providers().items()
        for process_id, process_config in provider_config.processes.items()
        if has_user_access_rights(
            process_id, provider_name, process_config, realm_roles, client_roles
        )
    ]

    tools = []

    async with aiohttp.ClientSession(
        raise_for_status=False, timeout=process_describe_timeout
    ) as session:
        tasks = [
            fetch_tool_description(session, provider_name, process_id)
            for provider_name, process_id in allowed_processes
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for (provider_name, process_id), result in zip(allowed_processes, results):
        if isinstance(result, BaseException):
            logger.error(
                "Error fetching process description for %s:%s: %s",
                provider_name,
                process_id,
                result,
            )
        else:
            tools.append(result)

    return {"tools": tools}


async def fetch_tool_description(
    session: ClientSession, provider_name: str, process_id: str
) -> dict:
    """Fetches one process's full description and maps it to an MCP tool entry."""
    provider_config: ProviderConfig = get_providers()[provider_name]

    headers = {
        "Content-type": "application/json",
        "Accept": "application/json",
    }

    auth_strategy = get_auth_strategy(provider_config.authentication)
    provider_auth = auth_strategy.get_auth()
    headers.update(provider_auth.headers)

    process_details = await fetch_json(
        session=session,
        url=f"{provider_config.server_url}processes/{process_id}",
        raise_for_status=True,
        headers=headers,
        auth=provider_auth.auth,
    )

    return {
        "tool": f"{provider_name}:{process_id}",
        "title": process_details.get("title") or process_id,
        "description": process_details.get("description", ""),
        "inputSchema": build_input_schema(process_details.get("inputs", {})),
        "provider": provider_name,
        "processId": process_id,
    }


def build_input_schema(inputs: dict) -> dict:
    """Maps an OGC API Processes 'inputs' description to a JSON Schema object,
    the shape MCP tool callers use to validate arguments before invocation."""
    properties = {}
    required = []

    for input_id, input_def in (inputs or {}).items():
        schema = dict(input_def.get("schema", {}))
        schema.setdefault("title", input_def.get("title"))
        schema.setdefault("description", input_def.get("description"))
        properties[input_id] = {k: v for k, v in schema.items() if v is not None}

        if is_input_required(input_def):
            required.append(input_id)

    input_schema = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required
    return input_schema


def is_input_required(input_def: dict) -> bool:
    if "required" in input_def:
        return bool(input_def["required"])
    if "required" in input_def.get("schema", {}):
        return bool(input_def["schema"]["required"])
    if "minOccurs" in input_def:
        return input_def["minOccurs"] > 0
    return False
