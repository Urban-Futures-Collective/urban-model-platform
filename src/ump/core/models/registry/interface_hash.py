"""Interface hash of an OGC process description (REF-F12, "Interface hash").

SHA-256 over canonical JSON (sorted keys, no whitespace, UTF-8) of
``{inputs, outputs, jobControlOptions, outputTransmission}``. The keys
``title``, ``description``, ``links`` and ``metadata`` are removed from every
definition object — each input/output definition and every nested schema
object — but not where keys are *names*: the ``inputs``/``outputs`` maps and
the JSON-Schema ``properties``, ``patternProperties``, ``$defs`` and
``definitions`` maps. Values of data keywords (``default``, ``enum``, …) are
kept verbatim. So text edits do not change the hash; schema, cardinality and
execution-mode changes do.

The hash must be computed from the raw remote description, before any of
UMP's own process handlers rewrite it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

HASHED_KEYS = ("inputs", "outputs", "jobControlOptions", "outputTransmission")
_TEXT_KEYS = frozenset({"title", "description", "links", "metadata"})
_NAME_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_DATA_KEYWORDS = frozenset({"default", "enum", "const", "example", "examples"})


def _definition(node: Any) -> Any:
    if isinstance(node, list):
        return [_definition(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _TEXT_KEYS:
            continue
        if key in _DATA_KEYWORDS:
            out[key] = value
        elif key in _NAME_MAPS and isinstance(value, dict):
            out[key] = {name: _definition(sub) for name, sub in value.items()}
        else:
            out[key] = _definition(value)
    return out


def _named_definitions(node: Any) -> Any:
    # inputs/outputs: a map of id -> definition (OGC API Processes 1.0)
    if isinstance(node, dict):
        return {name: _definition(sub) for name, sub in node.items()}
    return _definition(node)


def canonical_interface(description: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in HASHED_KEYS:
        if key not in description:
            continue
        value = description[key]
        out[key] = _named_definitions(value) if key in ("inputs", "outputs") else value
    return out


def interface_sha256(description: dict[str, Any]) -> str:
    canonical = json.dumps(
        canonical_interface(description),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
