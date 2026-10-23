"""The OGC API Processes surface (/v1.0/*) must not change (REF-F12 "API sketch").

Two guards:

1. Mounting the registry router leaves every /v1.0 path and every schema those
   paths reference byte-for-byte identical.
2. That OGC contract equals the committed snapshot
   ``tests/fixtures/ogc_openapi_v1.0.json``, generated from commit 511a7e9 —
   before the registry work. An intentional OGC change must update the
   snapshot in the same commit, which makes it visible in review.

Regenerate (only for a deliberate OGC change)::

    poetry run python tests/test_ogc_contract_unchanged.py \
        > tests/fixtures/ogc_openapi_v1.0.json
"""

import json
from pathlib import Path
from typing import Any, Dict

SNAPSHOT = Path(__file__).parent / "fixtures" / "ogc_openapi_v1.0.json"
OGC_PREFIX = "/v1.0"


def _refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)


def ogc_contract(openapi: Dict[str, Any]) -> Dict[str, Any]:
    """The /v1.0 paths plus every component schema they reach, transitively."""
    paths = {p: v for p, v in openapi["paths"].items() if p.startswith(OGC_PREFIX)}
    schemas = openapi.get("components", {}).get("schemas", {})
    wanted: set[str] = set()
    _refs(paths, wanted)
    resolved: Dict[str, Any] = {}
    while wanted - resolved.keys():
        name = sorted(wanted - resolved.keys())[0]
        resolved[name] = schemas.get(name)
        _refs(resolved[name], wanted)
    return {"paths": paths, "schemas": dict(sorted(resolved.items()))}


def _app(with_registry: bool):
    from ump.adapters.colon_process_id_validator import ColonProcessId
    from ump.adapters.job_repository_inmemory import InMemoryJobRepository
    from ump.adapters.web.fastapi import create_app
    from ump.core.services.authorization import AuthorizationService

    kwargs: Dict[str, Any] = {}
    if with_registry:
        from ump.adapters.registry_repository_inmemory import (
            InMemoryRegistryRepository,
        )
        from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
        from ump.adapters.safe_fetch_aiohttp import AioHttpSafeFetch
        from ump.adapters.web.registry_router import RegistryServices

        kwargs["registry"] = RegistryServices.create(
            InMemoryRegistryRepository(),
            authorization=AuthorizationService(None),  # type: ignore[arg-type]
            providers=None,  # type: ignore[arg-type]
            fetch=AioHttpSafeFetch(),
            remote_auth=RemoteAuthAdapter(),
            cipher=None,
            languages=["de", "en"],
            public_catalogue=False,
            jobs=InMemoryJobRepository("scratch/test_registry_jobs"),
        )
    # openapi() never runs the lifespan, so the factories are never called
    return create_app(
        process_manager_factory=lambda client: None,
        http_client=None,  # type: ignore[arg-type]
        job_manager_factory=lambda client, pm: None,
        job_repo=InMemoryJobRepository("scratch/test_ogc_contract"),
        process_id_validator=ColonProcessId(),
        **kwargs,
    )


def test_registry_router_does_not_touch_the_ogc_contract():
    with_registry = _app(with_registry=True).openapi()
    assert any(p.startswith("/registry/v1/") for p in with_registry["paths"])
    assert ogc_contract(with_registry) == ogc_contract(
        _app(with_registry=False).openapi()
    )


def test_ogc_contract_matches_snapshot():
    expected = json.loads(SNAPSHOT.read_text())
    assert ogc_contract(_app(with_registry=True).openapi()) == expected


if __name__ == "__main__":
    print(
        json.dumps(
            ogc_contract(_app(with_registry=False).openapi()), indent=2, sort_keys=True
        )
    )
