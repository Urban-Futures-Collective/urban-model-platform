"""ProvidersPort backed by the model registry (REF-F12, UMP_PROVIDERS_SOURCE=db).

``ProvidersPort`` is synchronous and sits on the hot path (ProcessManager,
JobManager, AuthorizationService), so the adapter never queries per call. It
serves an in-memory ``ProvidersSnapshot`` built from
``registry.v_providers_config`` and rebuilds it in the background:

  - once on startup (``async with`` — the web lifespan enters it before the
    first request),
  - whenever Postgres notifies ``ump_providers_changed`` (triggers from
    migration 0003 on provider_server and model), so a deactivation reaches
    every worker at once,
  - every ``poll_interval`` seconds as a fallback, e.g. while the LISTEN
    connection is being re-established,
  - when ``load_providers()`` is called (registry writes in this worker).

A failed rebuild keeps the previous snapshot, like the file adapter. A provider
whose row cannot be turned into a ProviderConfig (e.g. credentials that do not
decrypt) is left out and logged; the others still load.

The engine and the cipher are injected by the composition root.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ump.core.interfaces.credential_cipher import CredentialCipherPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.models.providers_config import (
    AuthConfig,
    NoAuthConfig,
    ProcessConfig,
    ProviderConfig,
    ProvidersSnapshot,
    auth_config_from_secret,
)

logger = logging.getLogger(__name__)

# Must match PROVIDERS_CHANNEL in migrations/versions/0003_registry_tables.py.
PROVIDERS_CHANNEL = "ump_providers_changed"

_SNAPSHOT_QUERY = text("""
    SELECT v.name, v.url, v.ttw_job_done, v.auth_type, v.processes, ps.credentials
      FROM registry.v_providers_config v
      JOIN registry.provider_server ps ON ps.id = v.provider_server_id
     ORDER BY v.name
    """)


class ProviderConfigDbAdapter(ProvidersPort):
    def __init__(
        self,
        engine: AsyncEngine,
        cipher: CredentialCipherPort | None,
        poll_interval: float = 30.0,
        reconnect_delay: float = 5.0,
    ) -> None:
        self._engine = engine
        self._cipher = cipher
        self._poll_interval = poll_interval
        self._reconnect_delay = reconnect_delay
        self._snapshot = ProvidersSnapshot()
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "ProviderConfigDbAdapter":
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        await self.refresh()
        self._tasks = [
            asyncio.create_task(self._refresh_loop(), name="providers-db-refresh"),
            asyncio.create_task(self._listen_loop(), name="providers-db-listen"),
        ]
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # ------------------------------------------------------------------
    # Snapshot building
    # ------------------------------------------------------------------

    async def refresh(self) -> bool:
        """Rebuild the snapshot now. Returns False (and keeps the old one) on error."""
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(_SNAPSHOT_QUERY)).mappings().all()
        except Exception as exc:
            logger.error("Could not load providers from the registry: %s", exc)
            return False

        providers: list[ProviderConfig] = []
        for row in rows:
            try:
                providers.append(self._to_provider(row))
            except Exception as exc:
                logger.error(
                    "Skipping provider %r from the registry: %s", row["name"], exc
                )
        self._snapshot = ProvidersSnapshot(providers)
        logger.info(
            "Providers (re)loaded from registry: %d provider(s): %s",
            len(providers),
            [p.name for p in providers],
        )
        return True

    def _to_provider(self, row: Any) -> ProviderConfig:
        processes = row["processes"]
        if isinstance(processes, str):
            processes = json.loads(processes)
        return ProviderConfig.model_validate(
            {
                "name": row["name"],
                "url": row["url"],
                "ttw-job-done": float(row["ttw_job_done"]),
                "authentication": self._authentication(
                    row["auth_type"], row["credentials"]
                ),
                "processes": processes,
            }
        )

    def _authentication(self, auth_type: str, credentials: bytes | None) -> AuthConfig:
        if auth_type == "NoAuth":
            return NoAuthConfig()
        if credentials is None:
            raise ValueError(f"{auth_type} server has no credentials stored")
        if self._cipher is None:
            raise ValueError(
                f"{auth_type} credentials need UMP_REGISTRY_CREDENTIALS_KEY"
            )
        secret = json.loads(self._cipher.decrypt(credentials))
        return auth_config_from_secret(auth_type, secret)

    # ------------------------------------------------------------------
    # Background refresh
    # ------------------------------------------------------------------

    def _request_refresh(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def _on_notify(self, *_args: Any) -> None:
        self._request_refresh()

    async def _refresh_loop(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
            self._wake.clear()
            await self.refresh()

    async def _listen_loop(self) -> None:
        reconnect = False
        while True:
            try:
                async with self._engine.connect() as conn:
                    raw = await conn.get_raw_connection()
                    driver = raw.driver_connection
                    lost = asyncio.Event()

                    def on_terminate(_connection: Any) -> None:
                        lost.set()

                    driver.add_termination_listener(on_terminate)
                    await driver.add_listener(PROVIDERS_CHANNEL, self._on_notify)
                    try:
                        if reconnect:
                            # catch up on changes made while not listening
                            self._request_refresh()
                        logger.info("Listening for registry changes")
                        await lost.wait()
                    finally:
                        driver.remove_termination_listener(on_terminate)
                        if not driver.is_closed():
                            await driver.remove_listener(
                                PROVIDERS_CHANNEL, self._on_notify
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Registry LISTEN connection failed (%s); retrying in %ss, "
                    "polling every %ss meanwhile",
                    exc,
                    self._reconnect_delay,
                    self._poll_interval,
                )
            reconnect = True
            await asyncio.sleep(self._reconnect_delay)

    # ------------------------------------------------------------------
    # ProvidersPort
    # ------------------------------------------------------------------

    def load_providers(self) -> None:
        """Schedule a rebuild of the snapshot (e.g. after a registry write)."""
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def get_providers(self) -> List[ProviderConfig]:
        return self._snapshot.providers

    def get_provider(self, provider_name: str) -> Optional[ProviderConfig]:
        return self._snapshot.get_provider(provider_name)

    def get_process_config(
        self, provider_name: str, process_id: str
    ) -> Optional[ProcessConfig]:
        return self._snapshot.get_process_config(provider_name, process_id)

    def list_providers(self) -> List[str]:
        return self._snapshot.provider_names()

    def get_processes(self, provider_name: str) -> List[str]:
        return self._snapshot.get_processes(provider_name)

    def check_process_availability(self, provider_name: str, process_id: str) -> bool:
        return self._snapshot.check_process_availability(provider_name, process_id)
