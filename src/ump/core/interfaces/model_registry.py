"""ModelRegistryPort — repository for the model registry (REF-F12).

Every write that changes registry state takes the ``AuditEvent`` describing
it and stores both in one transaction (decision 2026-10-19): either the change
and its audit row are committed, or neither is. Services decide what goes into
the event; repositories only persist it.

Updates are conditional on ``expected_updated_at`` (optimistic concurrency):
if the row changed since it was read, ``StaleWriteError`` is raised and nothing
is written.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.access import AccessRequest
from ump.core.models.registry.audit import AuditEvent


class RegistryConflictError(Exception):
    """A unique rule would be broken (server name, model slug, server+process,
    one open access request per user and model)."""


class StaleWriteError(Exception):
    """The row was changed by someone else since it was read."""


@dataclass(frozen=True)
class InterfaceCheck:
    """Result of one drift check of one model (REF-F12 "Interface drift").

    Applied only if the model's ``verified_interface_sha256`` still equals
    ``expected_verified_sha256`` — the hash the check compared against — so a
    re-verification that lands during a check is never overwritten. ``audit``
    is stored only if the check is applied.
    """

    model_id: UUID
    expected_verified_sha256: str | None
    interface_state: str
    current_interface_sha256: str | None
    last_checked_at: datetime
    audit: AuditEvent | None = None


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


@dataclass(frozen=True)
class AccessQuery:
    """Filter for ``list_access_requests``. Newest first: ``created_at`` desc,
    then ``id`` desc.

    A request matches when it is *visible* — ``visible_to is None``, or it is
    about that user, or its model is owned by that user — and passes every set
    filter. ``before`` is the keyset cursor ``(created_at, id)`` of the last
    request of the previous page.
    """

    visible_to: str | None = None  # None = every request (access admins)
    status: str | None = None
    model_id: UUID | None = None
    user_sub: str | None = None
    before: tuple[datetime, UUID] | None = None
    limit: int = 50

    def matches(self, request: AccessRequest, model_owner_sub: str | None) -> bool:
        """Reference semantics, used by the in-memory adapter and tests."""
        visible = self.visible_to is None or self.visible_to in (
            request.user_sub,
            model_owner_sub,
        )
        return (
            visible
            and (self.status is None or request.status == self.status)
            and (self.model_id is None or request.model_id == self.model_id)
            and (self.user_sub is None or request.user_sub == self.user_sub)
            and (self.before is None or (request.created_at, request.id) < self.before)
        )


class ModelRegistryPort(ABC):
    # --- accounts ---------------------------------------------------------

    @abstractmethod
    async def upsert_user(self, user: AppUser) -> AppUser:
        """Insert *user*, or refresh its cached claims and ``last_seen_at``.

        Claims that are None (``platform_roles``, ``lang``, …) are not
        refreshed; ``notify`` is never changed here (see ``set_notify``).
        """

    @abstractmethod
    async def ensure_user(self, user: AppUser) -> AppUser:
        """Insert *user* if the sub is unknown; never change an existing row.

        For accounts UMP learns about from someone else (a direct access grant),
        whose own token claims must win once they log in.
        """

    @abstractmethod
    async def get_users(self, subs: set[str]) -> dict[str, AppUser]:
        pass

    @abstractmethod
    async def list_users_with_role(self, role: str) -> list[AppUser]:
        """Users whose cached ``platform_roles`` hold *role* (mail recipients)."""

    @abstractmethod
    async def set_notify(self, sub: str, notify: dict[str, bool]) -> AppUser:
        """Replace the user's notification choices; the user must exist."""

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
    async def create_provider_server(
        self, server: ProviderServer, *, audit: AuditEvent
    ) -> ProviderServer:
        """Insert *server*; ``RegistryConflictError`` if the name is taken."""

    @abstractmethod
    async def update_provider_server(
        self, server: ProviderServer, *, expected_updated_at, audit: AuditEvent
    ) -> ProviderServer:
        """Store every field of *server* except ``name`` (immutable)."""

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
    async def get_model_by_slug(self, slug: str) -> RegistryModel | None:
        pass

    @abstractmethod
    async def create_models(
        self, models: list[RegistryModel], *, audit: list[AuditEvent]
    ) -> list[RegistryModel]:
        """Insert all *models* or none; ``RegistryConflictError`` on a duplicate."""

    @abstractmethod
    async def update_model(
        self, model: RegistryModel, *, expected_updated_at, audit: AuditEvent
    ) -> RegistryModel:
        """Store every field of *model* except identity (id, server, process)."""

    @abstractmethod
    async def record_interface_checks(
        self, checks: list[InterfaceCheck], *, audit: list[AuditEvent] = ()
    ) -> set[UUID]:
        """Store drift-check results and *audit* in one transaction.

        Writes only ``interface_state``, ``current_interface_sha256`` and
        ``last_checked_at``, and leaves ``updated_at`` (the ETag) alone: a
        periodic check must not invalidate the card a user is editing.
        Returns the ids of the models whose check was applied.
        """

    # --- access requests --------------------------------------------------

    @abstractmethod
    async def get_access_request(self, request_id: UUID) -> AccessRequest | None:
        pass

    @abstractmethod
    async def list_access_requests(self, query: AccessQuery) -> list[AccessRequest]:
        pass

    @abstractmethod
    async def create_access_request(
        self, request: AccessRequest, *, audit: AuditEvent
    ) -> AccessRequest:
        """Insert *request*; ``RegistryConflictError`` if the user already has
        an open (requested or granted) record for the model."""

    @abstractmethod
    async def update_access_request(
        self, request: AccessRequest, *, expected_updated_at, audit: AuditEvent
    ) -> AccessRequest:
        """Store the status fields of *request* (``status``, ``status_note``,
        ``status_changed_by``, ``status_changed_at``)."""
