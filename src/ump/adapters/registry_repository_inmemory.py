"""In-memory ModelRegistryPort for tests and database-less development.

Enforces the uniqueness rules of migration 0003 (server name, model slug,
server + remote process) so tests see the same conflicts as the database.
"""

from __future__ import annotations

from uuid import UUID

from ump.core.interfaces.model_registry import ModelRegistryPort
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel


class InMemoryRegistryRepository(ModelRegistryPort):
    def __init__(self) -> None:
        self.users: dict[str, AppUser] = {}
        self.servers: dict[str, ProviderServer] = {}
        self.models: dict[tuple[UUID, str], RegistryModel] = {}

    async def upsert_user(self, user: AppUser) -> None:
        existing = self.users.get(user.sub)
        if existing is None:
            self.users[user.sub] = user.model_copy()
            return
        updates = {k: v for k, v in user.model_dump().items() if v}
        self.users[user.sub] = existing.model_copy(update=updates)

    async def get_provider_server(self, name: str) -> ProviderServer | None:
        server = self.servers.get(name)
        return server.model_copy() if server else None

    async def create_provider_server(self, server: ProviderServer) -> ProviderServer:
        if server.name in self.servers:
            raise ValueError(f"provider_server {server.name!r} already exists")
        self.servers[server.name] = server.model_copy()
        return server

    async def get_model(
        self, provider_server_id: UUID, remote_process_id: str
    ) -> RegistryModel | None:
        model = self.models.get((provider_server_id, remote_process_id))
        return model.model_copy() if model else None

    async def create_model(self, model: RegistryModel) -> RegistryModel:
        key = (model.provider_server_id, model.remote_process_id)
        if key in self.models:
            raise ValueError(f"model {key!r} already exists")
        if any(m.slug == model.slug for m in self.models.values()):
            raise ValueError(f"model slug {model.slug!r} already exists")
        self.models[key] = model.model_copy()
        return model
