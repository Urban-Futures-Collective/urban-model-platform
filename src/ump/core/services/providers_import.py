"""ProvidersImportService — one-off migration of providers.yaml into the registry.

REF-F12 "Migration path": one ``provider_server`` per provider and one
``published`` model per listed (non-excluded) process, credentials encrypted,
owned by a given platform-admin ``sub``. Afterwards the deployment switches to
``UMP_PROVIDERS_SOURCE=db``.

The import is create-only and therefore idempotent: servers and models that
already exist are reported and left untouched, so re-running it never undoes
changes made in the registry since (e.g. a deactivation). Processes whose
description cannot be fetched are reported as failures; fix the server and
run the import again.

A published model must carry a verified interface (CHECK in 0003), so each
process description is fetched raw — through ``HttpClientPort`` and
``RemoteAuthPort``, not ``ProcessManager``, whose handlers rewrite it — and
stored with its interface hash. The verification is recorded as done by the
import (``IMPORT_ACTOR_SUB``): the processes were vetted when they were added
to providers.yaml.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ump.core.interfaces.credential_cipher import CredentialCipherPort
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.model_registry import ModelRegistryPort
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.models.providers_config import (
    ProcessConfig,
    ProviderConfig,
    auth_config_to_secret,
)
from ump.core.models.registry import (
    IMPORT_ACTOR_SUB,
    SLUG_PATTERN,
    AppUser,
    ProviderServer,
    RegistryModel,
    interface_sha256,
    slugify,
)

logger = logging.getLogger(__name__)

# Model-card fields providers.yaml has no source for. Both satisfy the
# completeness CHECK for published models and are visibly placeholders.
PLACEHOLDER_PURPOSE = "Imported from providers.yaml; model card not completed yet."
PLACEHOLDER_LICENSE = "NOASSERTION"  # SPDX: no licence information available

# ProcessConfig fields that live in their own registry columns, not in
# model.process_config.
_NOT_IN_PROCESS_CONFIG = {"id", "description", "version", "state", "exclude"}


@dataclass
class ImportReport:
    servers_created: list[str] = field(default_factory=list)
    servers_existing: list[str] = field(default_factory=list)
    models_created: list[str] = field(default_factory=list)
    models_existing: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


class ProvidersImportService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        http_client: HttpClientPort,
        remote_auth: RemoteAuthPort,
        cipher: CredentialCipherPort | None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._registry = registry
        self._http = http_client
        self._remote_auth = remote_auth
        self._cipher = cipher
        self._clock = clock

    async def import_providers(
        self,
        providers: list[ProviderConfig],
        owner_sub: str,
        default_lang: str = "de",
    ) -> ImportReport:
        if not owner_sub or owner_sub == IMPORT_ACTOR_SUB:
            raise ValueError(f"owner_sub must be a real account, not {owner_sub!r}")

        report = ImportReport()
        await self._registry.upsert_user(AppUser(sub=owner_sub))
        await self._registry.upsert_user(
            AppUser(sub=IMPORT_ACTOR_SUB, display_name="providers.yaml import")
        )

        for provider in providers:
            try:
                server = await self._import_server(provider, owner_sub, report)
            except Exception as exc:  # one bad provider must not stop the rest
                report.failures.append(f"{provider.name}: {exc}")
                continue
            for process in provider.processes:
                if process.exclude:
                    continue
                label = f"{provider.name}:{process.id}"
                try:
                    await self._import_process(
                        provider, server, process, owner_sub, default_lang, report
                    )
                except Exception as exc:
                    report.failures.append(f"{label}: {exc}")
        return report

    async def _import_server(
        self, provider: ProviderConfig, owner_sub: str, report: ImportReport
    ) -> ProviderServer:
        existing = await self._registry.get_provider_server(provider.name)
        if existing is not None:
            report.servers_existing.append(provider.name)
            return existing

        if not SLUG_PATTERN.match(provider.name):
            raise ValueError(
                "provider name must be lower-case letters, digits and single "
                "hyphens; it prefixes process ids and roles, so it is not renamed "
                "automatically"
            )

        auth_type, secret = auth_config_to_secret(provider.authentication)
        credentials = None
        if secret is not None:
            if self._cipher is None:
                raise ValueError(
                    f"{auth_type} credentials need UMP_REGISTRY_CREDENTIALS_KEY"
                )
            credentials = self._cipher.encrypt(json.dumps(secret))

        server = await self._registry.create_provider_server(
            ProviderServer(
                name=provider.name,
                base_url=str(provider.url),
                owner_sub=owner_sub,
                auth_type=auth_type,
                credentials=credentials,
                ttw_job_done_seconds=provider.ttw_job_done,
            )
        )
        report.servers_created.append(provider.name)
        return server

    async def _import_process(
        self,
        provider: ProviderConfig,
        server: ProviderServer,
        process: ProcessConfig,
        owner_sub: str,
        lang: str,
        report: ImportReport,
    ) -> None:
        label = f"{provider.name}:{process.id}"
        if await self._registry.get_model(server.id, process.id) is not None:
            report.models_existing.append(label)
            return

        description = await self._fetch_description(provider, process.id)
        sha = interface_sha256(description)
        now = self._clock()
        title = description.get("title")
        summary = description.get("description")
        keywords = description.get("keywords")

        await self._registry.create_model(
            RegistryModel(
                slug=slugify(f"{provider.name}-{process.id}"),
                provider_server_id=server.id,
                remote_process_id=process.id,
                owner_sub=owner_sub,
                default_lang=lang,
                # providers.yaml `description` is what clients see today, so it
                # wins over the remote title for the name
                full_name={lang: process.description or title or process.id},
                short_description={lang: summary} if summary else {},
                purpose={lang: summary or PLACEHOLDER_PURPOSE},
                keywords=(
                    {lang: [str(k) for k in keywords]}
                    if isinstance(keywords, list) and keywords
                    else {}
                ),
                version_label=process.version or description.get("version"),
                license=PLACEHOLDER_LICENSE,
                process_config=process.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude=_NOT_IN_PROCESS_CONFIG,
                    exclude_none=True,
                ),
                status="published",
                status_changed_by=IMPORT_ACTOR_SUB,
                status_changed_at=now,
                published_at=now,
                verified_by=IMPORT_ACTOR_SUB,
                verified_at=now,
                verified_interface=description,
                verified_interface_sha256=sha,
                current_interface_sha256=sha,
                interface_state="in_sync",
                last_checked_at=now,
            )
        )
        report.models_created.append(label)

    async def _fetch_description(
        self, provider: ProviderConfig, process_id: str
    ) -> dict[str, Any]:
        url = str(provider.url).rstrip("/") + f"/processes/{process_id}"
        headers = self._remote_auth.resolve(provider.authentication).headers
        description = await self._http.get(url, headers=headers or None)
        if not isinstance(description, dict):
            raise ValueError(f"process description at {url} is not a JSON object")
        return description
