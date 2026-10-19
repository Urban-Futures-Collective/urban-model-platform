"""ModelRegistryPort — repository for the model registry (REF-F12).

Started with what `ump-import-providers` needs; the submission and
verification workflows will extend it.
"""

from abc import ABC, abstractmethod
from uuid import UUID

from ump.core.models.registry import AppUser, ProviderServer, RegistryModel


class ModelRegistryPort(ABC):
    @abstractmethod
    async def upsert_user(self, user: AppUser) -> None:
        """Insert *user*, or refresh its cached claims if it exists."""

    @abstractmethod
    async def get_provider_server(self, name: str) -> ProviderServer | None:
        """The server with this (unique, immutable) name, if any."""

    @abstractmethod
    async def create_provider_server(self, server: ProviderServer) -> ProviderServer:
        pass

    @abstractmethod
    async def get_model(
        self, provider_server_id: UUID, remote_process_id: str
    ) -> RegistryModel | None:
        pass

    @abstractmethod
    async def create_model(self, model: RegistryModel) -> RegistryModel:
        pass
