"""UMP command-line entry points.

Registered as Poetry scripts in pyproject.toml:
  ump          → main()         start the API server
  ump-migrate  → migrate()      run Alembic migrations (upgrade head)
  ump-import-providers → import_providers()  copy providers.yaml into the registry
"""

import sys


def migrate() -> None:
    """Run ``alembic upgrade head`` using credentials from environment variables.

    Reads, in priority order:
      1. UMP_DATABASE_URL          full DSN (asyncpg prefix stripped automatically)
      2. UMP_DATABASE_HOST / PORT / USER / PASSWORD / NAME   individual vars
      3. alembic.ini placeholder   (will fail — only useful for dry-runs)
    """
    from alembic.config import Config
    from alembic.config import main as alembic_main

    # Pass any extra CLI args through (e.g. ump-migrate downgrade base)
    argv = sys.argv[1:] if len(sys.argv) > 1 else ["upgrade", "head"]
    alembic_main(argv=argv)


def main() -> None:
    from ump.main import main as _main

    _main()


def import_providers() -> None:
    """Copy providers.yaml into the model registry (REF-F12 migration path).

    Create-only and safe to re-run: existing servers and models are skipped.
    Exits non-zero if anything could not be imported. Afterwards set
    UMP_PROVIDERS_SOURCE=db.
    """
    import argparse
    import asyncio

    from ump.core.settings import app_settings

    parser = argparse.ArgumentParser(
        prog="ump-import-providers",
        description="Import providers.yaml into the model registry database.",
    )
    parser.add_argument(
        "--file",
        default=str(app_settings.UMP_PROVIDERS_FILE),
        help="providers file to import (default: UMP_PROVIDERS_FILE)",
    )
    parser.add_argument(
        "--owner-sub",
        required=True,
        help="Keycloak user id (JWT sub) of the platform admin owning the models",
    )
    parser.add_argument(
        "--default-lang",
        default="de",
        help="language of the imported model-card texts (default: de)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_import_providers(args.file, args.owner_sub, args.default_lang)))


async def _import_providers(path: str, owner_sub: str, default_lang: str) -> int:
    # Composition root of this command: build the adapters, inject them into
    # the core service, run it.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from ump.adapters.aiohttp_client_adapter import AioHttpClientAdapter
    from ump.adapters.provider_config_file_adapter import ProviderConfigFileAdapter
    from ump.adapters.registry_repository_sql import SQLModelRegistryRepository
    from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
    from ump.composition import build_credential_cipher, construct_database_url
    from ump.core.services.providers_import import ProvidersImportService

    providers = ProviderConfigFileAdapter(path).get_providers()
    if not providers:
        print(f"No providers loaded from {path}; nothing to import.", file=sys.stderr)
        return 1

    engine = create_async_engine(construct_database_url())
    try:
        registry = SQLModelRegistryRepository(
            async_sessionmaker(engine, expire_on_commit=False)
        )
        async with AioHttpClientAdapter() as http_client:
            service = ProvidersImportService(
                registry,
                http_client,
                RemoteAuthAdapter(),
                build_credential_cipher(),
            )
            report = await service.import_providers(
                providers, owner_sub, default_lang=default_lang
            )
    finally:
        await engine.dispose()

    for title, items in (
        ("Servers created", report.servers_created),
        ("Servers already present (left unchanged)", report.servers_existing),
        ("Models created (published)", report.models_created),
        ("Models already present (left unchanged)", report.models_existing),
        ("FAILED", report.failures),
    ):
        print(f"{title}: {len(items)}")
        for item in items:
            print(f"  - {item}")
    return 0 if report.ok else 1
