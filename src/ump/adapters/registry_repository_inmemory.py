"""In-memory ModelRegistryPort for tests and database-less development.

Enforces the uniqueness rules of migration 0003 (server name, model slug,
server + remote process) so tests see the same conflicts as the database, and
fills the timestamps the database would set.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from ump.core.interfaces.model_registry import ModelQuery, ModelRegistryPort
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel


class InMemoryRegistryRepository(ModelRegistryPort):
    def __init__(
        self, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    ) -> None:
        self._clock = clock
        self.users: dict[str, AppUser] = {}
        self.servers: dict[str, ProviderServer] = {}
        self.models: dict[tuple[UUID, str], RegistryModel] = {}

    # --- accounts ---------------------------------------------------------

    async def upsert_user(self, user: AppUser) -> AppUser:
        now = self._clock()
        existing = self.users.get(user.sub)
        if existing is None:
            stored = user.model_copy(update={"first_seen_at": now, "last_seen_at": now})
        else:
            updates = {
                k: v
                for k, v in user.model_dump(include={"display_name", "email"}).items()
                if v
            }
            stored = existing.model_copy(update={**updates, "last_seen_at": now})
        self.users[user.sub] = stored
        return stored.model_copy()

    # --- provider servers -------------------------------------------------

    async def get_provider_server(self, name: str) -> ProviderServer | None:
        server = self.servers.get(name)
        return server.model_copy() if server else None

    async def get_provider_server_by_id(self, server_id: UUID) -> ProviderServer | None:
        for server in self.servers.values():
            if server.id == server_id:
                return server.model_copy()
        return None

    async def get_provider_servers_by_ids(
        self, server_ids: set[UUID]
    ) -> dict[UUID, ProviderServer]:
        return {
            s.id: s.model_copy() for s in self.servers.values() if s.id in server_ids
        }

    async def list_provider_servers(
        self, owner_sub: str | None = None
    ) -> list[ProviderServer]:
        return [
            s.model_copy()
            for name, s in sorted(self.servers.items())
            if owner_sub is None or s.owner_sub == owner_sub
        ]

    async def create_provider_server(self, server: ProviderServer) -> ProviderServer:
        if server.name in self.servers:
            raise ValueError(f"provider_server {server.name!r} already exists")
        now = self._clock()
        stored = server.model_copy(update={"created_at": now, "updated_at": now})
        self.servers[server.name] = stored
        return stored.model_copy()

    # --- models -----------------------------------------------------------

    async def get_model(
        self, provider_server_id: UUID, remote_process_id: str
    ) -> RegistryModel | None:
        model = self.models.get((provider_server_id, remote_process_id))
        return model.model_copy() if model else None

    async def get_model_by_id(self, model_id: UUID) -> RegistryModel | None:
        for model in self.models.values():
            if model.id == model_id:
                return model.model_copy()
        return None

    async def list_models(self, query: ModelQuery) -> list[RegistryModel]:
        matching = sorted(
            (m for m in self.models.values() if query.matches(m)),
            key=lambda m: m.slug,
        )
        return [m.model_copy() for m in matching[: query.limit]]

    async def list_published_process_ids(self) -> list[tuple[str, str]]:
        names = {s.id: s.name for s in self.servers.values()}
        return sorted(
            (names[m.provider_server_id], m.remote_process_id)
            for m in self.models.values()
            if m.published_at is not None
        )

    async def create_model(self, model: RegistryModel) -> RegistryModel:
        key = (model.provider_server_id, model.remote_process_id)
        if key in self.models:
            raise ValueError(f"model {key!r} already exists")
        if any(m.slug == model.slug for m in self.models.values()):
            raise ValueError(f"model slug {model.slug!r} already exists")
        now = self._clock()
        stored = model.model_copy(update={"created_at": now, "updated_at": now})
        self.models[key] = stored
        return stored.model_copy()
