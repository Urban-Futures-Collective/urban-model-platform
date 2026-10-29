"""In-memory ModelRegistryPort for tests and database-less development.

Enforces the uniqueness rules of migrations 0003/0004 (server name, model slug,
server + remote process, one open access request per user and model) and the
optimistic-concurrency rule of the port, so
tests see the same conflicts as the database. Fills the timestamps the
database would set, and keeps the audit events in ``audit`` (written together
with the change, or not at all — like the SQL transaction). Given an
``InMemoryAuditLog`` (injected by the composition root), it also appends them
there, so the audit API sees registry changes like it does with a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

from ump.adapters.audit_log_inmemory import InMemoryAuditLog
from ump.core.interfaces.model_registry import (
    AccessQuery,
    InterfaceCheck,
    ModelQuery,
    ModelRegistryPort,
    RegistryConflictError,
    StaleWriteError,
)
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.access import OPEN_STATUSES, AccessRequest
from ump.core.models.registry.audit import AuditEvent


class InMemoryRegistryRepository(ModelRegistryPort):
    def __init__(
        self,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        audit_log: InMemoryAuditLog | None = None,
    ) -> None:
        self._clock = clock
        self._audit_log = audit_log
        self.users: dict[str, AppUser] = {}
        self.servers: dict[str, ProviderServer] = {}
        self.models: dict[tuple[UUID, str], RegistryModel] = {}
        self.access_requests: dict[UUID, AccessRequest] = {}
        self.audit: list[AuditEvent] = []
        self._last_now: datetime | None = None

    def _now(self) -> datetime:
        # strictly increasing, so every write changes updated_at (and the ETag)
        now = self._clock()
        last = self._last_now
        if last is not None and now <= last:
            now = last + timedelta(microseconds=1)
        self._last_now = now
        return now

    async def _record(self, events: list[AuditEvent]) -> None:
        self.audit.extend(events)
        if self._audit_log is not None:
            for event in events:
                await self._audit_log.append(event)

    # --- accounts ---------------------------------------------------------

    async def upsert_user(self, user: AppUser) -> AppUser:
        now = self._now()
        existing = self.users.get(user.sub)
        if existing is None:
            stored = user.model_copy(
                update={
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "platform_roles": user.platform_roles or [],
                    "notify": {},
                }
            )
        else:
            updates = {
                k: v
                for k, v in user.model_dump(
                    include={"display_name", "email", "lang"}
                ).items()
                if v
            }
            if user.platform_roles is not None:
                updates["platform_roles"] = sorted(user.platform_roles)
            stored = existing.model_copy(update={**updates, "last_seen_at": now})
        self.users[user.sub] = stored
        return stored.model_copy()

    async def ensure_user(self, user: AppUser) -> AppUser:
        if user.sub not in self.users:
            now = self._now()
            self.users[user.sub] = user.model_copy(
                update={
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "platform_roles": user.platform_roles or [],
                    "notify": {},
                }
            )
        return self.users[user.sub].model_copy()

    async def get_users(self, subs: set[str]) -> dict[str, AppUser]:
        return {s: u.model_copy() for s, u in self.users.items() if s in subs}

    async def list_users_with_role(self, role: str) -> list[AppUser]:
        return [
            u.model_copy()
            for _, u in sorted(self.users.items())
            if role in (u.platform_roles or [])
        ]

    async def set_notify(self, sub: str, notify: dict[str, bool]) -> AppUser:
        user = self.users[sub]  # KeyError: callers upsert the caller first
        self.users[sub] = user.model_copy(update={"notify": dict(notify)})
        return self.users[sub].model_copy()

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

    async def create_provider_server(
        self, server: ProviderServer, *, audit: AuditEvent
    ) -> ProviderServer:
        if server.name in self.servers:
            raise RegistryConflictError(f"provider_server {server.name!r} exists")
        now = self._now()
        stored = server.model_copy(update={"created_at": now, "updated_at": now})
        self.servers[server.name] = stored
        await self._record([audit])
        return stored.model_copy()

    async def update_provider_server(
        self, server: ProviderServer, *, expected_updated_at, audit: AuditEvent
    ) -> ProviderServer:
        current = await self.get_provider_server_by_id(server.id)
        if current is None or current.updated_at != expected_updated_at:
            raise StaleWriteError(str(server.id))
        stored = server.model_copy(
            update={
                "name": current.name,
                "created_at": current.created_at,
                "updated_at": self._now(),
            }
        )
        self.servers[current.name] = stored
        await self._record([audit])
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

    async def get_model_by_slug(self, slug: str) -> RegistryModel | None:
        for model in self.models.values():
            if model.slug == slug:
                return model.model_copy()
        return None

    async def create_models(
        self, models: list[RegistryModel], *, audit: list[AuditEvent]
    ) -> list[RegistryModel]:
        slugs = {m.slug for m in self.models.values()}
        keys = set(self.models)
        for model in models:  # check everything first: all or nothing
            key = (model.provider_server_id, model.remote_process_id)
            if key in keys or model.slug in slugs:
                raise RegistryConflictError(f"model {model.slug!r} exists")
            keys.add(key)
            slugs.add(model.slug)
        now = self._now()
        stored = []
        for model in models:
            row = model.model_copy(update={"created_at": now, "updated_at": now})
            self.models[(model.provider_server_id, model.remote_process_id)] = row
            stored.append(row.model_copy())
        await self._record(audit)
        return stored

    async def update_model(
        self, model: RegistryModel, *, expected_updated_at, audit: AuditEvent
    ) -> RegistryModel:
        current = await self.get_model_by_id(model.id)
        if current is None or current.updated_at != expected_updated_at:
            raise StaleWriteError(str(model.id))
        stored = model.model_copy(
            update={
                "provider_server_id": current.provider_server_id,
                "remote_process_id": current.remote_process_id,
                "created_at": current.created_at,
                "updated_at": self._now(),
            }
        )
        self.models[(current.provider_server_id, current.remote_process_id)] = stored
        await self._record([audit])
        return stored.model_copy()

    async def record_interface_checks(
        self, checks: list[InterfaceCheck], *, audit: list[AuditEvent] = ()
    ) -> set[UUID]:
        applied: set[UUID] = set()
        events = list(audit)
        for check in checks:
            current = await self.get_model_by_id(check.model_id)
            if (
                current is None
                or current.verified_interface_sha256 != check.expected_verified_sha256
            ):
                continue
            key = (current.provider_server_id, current.remote_process_id)
            self.models[key] = current.model_copy(
                update={
                    "interface_state": check.interface_state,
                    "current_interface_sha256": check.current_interface_sha256,
                    "last_checked_at": check.last_checked_at,
                }
            )  # updated_at unchanged
            applied.add(check.model_id)
            if check.audit is not None:
                events.append(check.audit)
        await self._record(events)
        return applied

    # --- access requests --------------------------------------------------

    async def get_access_request(self, request_id: UUID) -> AccessRequest | None:
        request = self.access_requests.get(request_id)
        return request.model_copy() if request else None

    async def list_access_requests(self, query: AccessQuery) -> list[AccessRequest]:
        owners = {m.id: m.owner_sub for m in self.models.values()}
        matching = sorted(
            (
                r
                for r in self.access_requests.values()
                if query.matches(r, owners.get(r.model_id))
            ),
            key=lambda r: (r.created_at, r.id),
            reverse=True,
        )
        return [r.model_copy() for r in matching[: query.limit]]

    def _check_access_request(self, request: AccessRequest) -> None:
        # the foreign keys and the partial unique index of migration 0004
        if request.user_sub not in self.users:
            raise ValueError(f"unknown user {request.user_sub!r}")
        if request.status in OPEN_STATUSES and any(
            r.id != request.id
            and r.user_sub == request.user_sub
            and r.model_id == request.model_id
            and r.status in OPEN_STATUSES
            for r in self.access_requests.values()
        ):
            raise RegistryConflictError("an open access request exists")

    async def create_access_request(
        self, request: AccessRequest, *, audit: AuditEvent
    ) -> AccessRequest:
        if request.id in self.access_requests:
            raise RegistryConflictError(f"access_request {request.id} exists")
        self._check_access_request(request)
        now = self._now()
        stored = request.model_copy(
            update={
                "status_changed_at": request.status_changed_at or now,
                "created_at": now,
                "updated_at": now,
            }
        )
        self.access_requests[request.id] = stored
        await self._record([audit])
        return stored.model_copy()

    async def update_access_request(
        self, request: AccessRequest, *, expected_updated_at, audit: AuditEvent
    ) -> AccessRequest:
        current = self.access_requests.get(request.id)
        if current is None or current.updated_at != expected_updated_at:
            raise StaleWriteError(str(request.id))
        self._check_access_request(request)
        stored = current.model_copy(
            update={
                "status": request.status,
                "status_note": request.status_note,
                "status_changed_by": request.status_changed_by,
                "status_changed_at": request.status_changed_at,
                "updated_at": self._now(),
            }
        )
        self.access_requests[request.id] = stored
        await self._record([audit])
        return stored.model_copy()
