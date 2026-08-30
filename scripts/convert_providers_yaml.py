#!/usr/bin/env python3
"""Convert a v2 (dict-keyed) providers.yaml to the v3 (list-based) format.

v2 keyed providers by a mapping key and processes by a nested mapping:

    modelserver-1:
      name: "CSL Test Modelserver"
      url: "https://example.org/"
      processes:
        abm-test-model:
          anonymous-access: true

v3 expects a list under a `providers:` key, with the process list carrying `id`:

    providers:
      - name: modelserver-1
        url: "https://example.org/"
        processes:
          - id: abm-test-model
            anonymous-access: true

Why the mapping key becomes `name` (and the old `name:` is dropped): in v2 the
*key* was the prefix in the canonical process id (`processes.py`:
``process["id"] = f"{provider_name}:{process_id}"``), while in v3 the prefix is
the `name` field. Carrying over the old human-readable `name` instead would
silently change every process id — and break any id containing a space.

Usage:
    python scripts/convert_providers_yaml.py old.yaml > new.yaml
    python scripts/convert_providers_yaml.py old.yaml -o new.yaml
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List

import yaml

# Provider-level keys v3 understands; anything else is reported, not silently
# dropped. `timeout` was v2-only and has no v3 equivalent (see ttw-job-done).
PROVIDER_KEYS = {"name", "url", "authentication", "ttw-job-done", "processes"}


def convert(raw: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    notes: List[str] = []

    if "providers" in raw:
        raise SystemExit("Input already looks like the v3 format (has 'providers:').")

    providers: List[Dict[str, Any]] = []

    for key, cfg in raw.items():
        if not isinstance(cfg, dict):
            notes.append(f"skipped top-level entry {key!r}: not a mapping")
            continue

        provider: Dict[str, Any] = {"name": key}

        if cfg.get("name") and cfg["name"] != key:
            notes.append(
                f"{key}: dropped display name {cfg['name']!r} — v3 uses 'name' as "
                f"the process-id prefix, so it must stay {key!r}"
            )

        if "url" in cfg:
            provider["url"] = cfg["url"]
        else:
            notes.append(f"{key}: no 'url' — v3 requires one")

        if "authentication" in cfg:
            provider["authentication"] = cfg["authentication"]

        if "timeout" in cfg:
            notes.append(
                f"{key}: 'timeout: {cfg['timeout']}' has no v3 equivalent and was "
                f"dropped; the closest setting is 'ttw-job-done' (seconds to wait "
                f"for a remote job before failing it)"
            )

        for unknown in set(cfg) - PROVIDER_KEYS - {"timeout"}:
            notes.append(f"{key}: unknown provider key {unknown!r} was dropped")

        processes_in = cfg.get("processes") or {}
        processes_out: List[Dict[str, Any]] = []

        if isinstance(processes_in, dict):
            for process_id, process_cfg in processes_in.items():
                entry: Dict[str, Any] = {"id": process_id}
                if isinstance(process_cfg, dict):
                    entry.update(process_cfg)
                elif process_cfg is not None:
                    notes.append(
                        f"{key}:{process_id}: unexpected process config "
                        f"{process_cfg!r}, kept id only"
                    )
                if entry.get("result-storage") == "geoserver":
                    notes.append(
                        f"{key}:{process_id}: 'result-storage: geoserver' is parsed "
                        f"but inert — v3 has no result-storage implementation yet"
                    )
                processes_out.append(entry)
        elif isinstance(processes_in, list):
            notes.append(f"{key}: 'processes' was already a list, copied as-is")
            processes_out = processes_in
        else:
            notes.append(f"{key}: no processes configured")

        provider["processes"] = processes_out
        providers.append(provider)

    return {"providers": providers}, notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="v2 providers.yaml")
    parser.add_argument("-o", "--output", help="write here instead of stdout")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    converted, notes = convert(raw)
    text = yaml.safe_dump(converted, sort_keys=False, allow_unicode=True)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)

    for note in notes:
        print(f"note: {note}", file=sys.stderr)


if __name__ == "__main__":
    main()
