"""Summary of how a process interface changed (REF-F12 "Interface drift").

Compares the canonical interfaces (see ``interface_hash``), so it reports
exactly the changes that change the hash: text-only edits never show up.
"""

from __future__ import annotations

from typing import Any

from ump.core.models.registry.interface_hash import canonical_interface


def _named(before: Any, after: Any) -> dict[str, list[str]]:
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }


def interface_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """``{inputs|outputs: {added, removed, changed}, <key>: {before, after}}``.

    Only sections that differ are present; equal interfaces give ``{}``.
    """
    old, new = canonical_interface(before), canonical_interface(after)
    out: dict[str, Any] = {}
    for key in ("inputs", "outputs"):
        changes = _named(old.get(key), new.get(key))
        if any(changes.values()):
            out[key] = changes
    for key in ("jobControlOptions", "outputTransmission"):
        if old.get(key) != new.get(key):
            out[key] = {"before": old.get(key), "after": new.get(key)}
    return out
