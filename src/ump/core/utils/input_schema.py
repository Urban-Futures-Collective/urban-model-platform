"""OGC API Processes ``inputs`` → JSON Schema.

Pure functions: no I/O, no ports, no framework. MCP tool callers validate their
arguments against the resulting schema before invoking a tool, so the mapping
has to be faithful about which inputs are required.

Ported from the v2 prototype (``feat/mcp:src/ump/api/mcp.py``), which got the
required-detection right; only the transport around it was discarded.
See ``reports/REF-F10-mcp-endpoint.md``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


def is_input_required(input_def: Mapping[str, Any]) -> bool:
    """Whether an OGC input description marks the input as required.

    Providers express this inconsistently, so all three spellings are honoured,
    most explicit first: an ``required`` flag on the input, the same flag inside
    its ``schema``, and finally OGC's own ``minOccurs``.
    """
    if "required" in input_def:
        return bool(input_def["required"])

    schema = input_def.get("schema")
    if isinstance(schema, Mapping) and "required" in schema:
        return bool(schema["required"])

    if "minOccurs" in input_def:
        try:
            return int(input_def["minOccurs"]) > 0
        except (TypeError, ValueError):
            return False

    # OGC defaults minOccurs to 1, but an input description that says nothing at
    # all is treated as optional: advertising a required argument that the
    # provider does not enforce would make the tool uncallable.
    return False


def ogc_inputs_to_json_schema(
    inputs: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Map an OGC ``inputs`` description to a JSON Schema object.

    Each input's own ``schema`` is used as the property schema, enriched with the
    input's ``title``/``description`` when the schema does not carry its own.
    Inputs without a usable schema still appear as unconstrained properties, so a
    sparse provider description degrades to a callable — if permissive — tool.
    """
    properties: Dict[str, Any] = {}
    required: list[str] = []

    for input_id, input_def in (inputs or {}).items():
        if not isinstance(input_def, Mapping):
            continue

        raw_schema = input_def.get("schema")
        schema: Dict[str, Any] = (
            dict(raw_schema) if isinstance(raw_schema, Mapping) else {}
        )

        schema.setdefault("title", input_def.get("title"))
        schema.setdefault("description", input_def.get("description"))

        # A property schema of {} is valid JSON Schema (accepts anything); drop
        # only the keys we could not fill in.
        properties[input_id] = {k: v for k, v in schema.items() if v is not None}

        if is_input_required(input_def):
            required.append(input_id)

    json_schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        json_schema["required"] = required
    return json_schema
