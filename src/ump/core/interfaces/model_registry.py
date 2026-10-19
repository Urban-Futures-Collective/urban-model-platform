"""ModelRegistryPort — repository for the model registry (REF-F12).

Writes that change registry state will take an ``AuditEvent`` and record it in
the same transaction (decision 2026-10-19); the read methods below need none.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID

from ump.core.models.registry import AppUser, ProviderServer, RegistryModel


@dataclass(frozen=True)
class ModelQuery:
    """Filter for ``list_models``. Results are ordered by slug.

    A model matches when it is *visible* —
    ``visible_statuses is None or status in visible_statuses or
    owner_sub == visible_owner`` — and passes every set filter
    (``status``, ``owner_sub``, ``provider_server_id``, ``after_slug``).
    """

    visible_statuses: frozenset[str] | None = None  # None = every status
    visible_owner: str | None = None  # also sees all models of this owner
    status: str | None = None
    owner_sub: str | None = None
    provider_server_id: UUID | None = None
    after_slug: str | None = None  # keyset pagination cursor
    limit: int = 50

    def matches(self, model: RegistryModel) -> bool:
        """Reference semantics, used by the in-memory adapter and tests."""
        visible = (
            self.visible_statuses is None
            or model.status in self.visible_statuses
            or (
                self.visible_owner is not None and model.owner_sub == self.visible_owner
            )
        )
        return (
            visible
            and (self.status is None or model.status == self.status)
            and (self.owner_sub is None or model.owner_sub == self.owner_sub)
            and (
                self.provider_server_id is None
                or model.provider_server_id == self.provider_server_id
            )
            and (self.after_slug is None or model.slug > self.after_slug)
        )


class ModelRegistryPort(ABC):
    # --- accounts ---------------------------------------------------------

    @abstractmethod
    async def upsert_user(self, user: AppUser) -> AppUser:
        """Insert *user*, or refresh its cached claims and ``last_seen_at``."""

    # --- provider servers -------------------------------------------------

    @abstractmethod
    async def get_provider_server(self, name: str) -> ProviderServer | None:
        """The server with this (unique, immutable) name, if any."""

    @abstractmethod
    async def get_provider_server_by_id(self, server_id: UUID) -> ProviderServer | None:
        pass

    @abstractmethod
    async def get_provider_servers_by_ids(
        self, server_ids: set[UUID]
    ) -> dict[UUID, ProviderServer]:
        pass

    @abstractmethod
    async def list_provider_servers(
        self, owner_sub: str | None = None
    ) -> list[ProviderServer]:
        """All servers ordered by name, or only those of *owner_sub*."""

    @abstractmethod
    async def create_provider_server(self, server: ProviderServer) -> ProviderServer:
        pass

    # --- models -----------------------------------------------------------

    @abstractmethod
    async def get_model(
        self, provider_server_id: UUID, remote_process_id: str
    ) -> RegistryModel | None:
        pass

    @abstractmethod
    async def get_model_by_id(self, model_id: UUID) -> RegistryModel | None:
        pass

    @abstractmethod
    async def list_models(self, query: ModelQuery) -> list[RegistryModel]:
        pass

    @abstractmethod
    async def list_published_process_ids(self) -> list[tuple[str, str]]:
        """(server name, remote process id) of every model ever published."""

    @abstractmethod
    async def create_model(self, model: RegistryModel) -> RegistryModel:
        pass
