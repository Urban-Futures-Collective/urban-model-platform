"""Read models the registry API returns (REF-F12 "API sketch").

Plain Pydantic in the core, like ``ToolCatalog`` for the MCP router: the web
adapter serialises them as they are. Credentials never appear in any view.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from ump.core.models.registry.audit import AuditRecord


class ReviewInfo(BaseModel):
    """Review details, shown to the owner, verifiers and platform admins only."""

    status_note: str | None = None
    status_changed_by: str | None = None
    status_changed_at: datetime | None = None
    verified_by: str | None = None
    verified_at: datetime | None = None
    verification_job_id: str | None = None


class ModelCardView(BaseModel):
    """A model card in one language (``lang``), or all languages (``lang='*'``).

    With a single language every language-map field holds that language's text
    (falling back to ``default_lang``); with ``'*'`` it holds the full map.
    """

    id: UUID
    slug: str
    process_id: str  # OGC process id, `{provider}:{remote id}`
    provider: str
    remote_process_id: str
    owner_sub: str
    # The owner's name as cached from their token (app_user); visible to every
    # caller who may see the model. None until the owner has used the registry.
    owner_display_name: str | None = None
    lang: str
    languages: list[str]
    default_lang: str
    full_name: Any
    short_description: Any = None
    purpose: Any = None
    limitations_risks: Any = None
    keywords: Any = None
    version_label: str | None = None
    repository_url: str | None = None
    code_ref: str | None = None
    license: str | None = None
    requirements: dict[str, Any] = {}
    contributors: list[dict[str, Any]] = []
    literature: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    test_cases: list[dict[str, Any]] = []
    io_notes: dict[str, Any] = {}
    status: str
    # process_config "anonymous-access": runnable without a model-access role
    anonymous_access: bool = False
    exec_state: str | None = None
    interface_state: str
    published_at: datetime | None = None
    updated_at: datetime | None = None
    review: ReviewInfo | None = None


class ModelPage(BaseModel):
    models: list[ModelCardView]
    next_cursor: str | None = None


class ProviderServerView(BaseModel):
    id: UUID
    name: str
    base_url: str
    owner_sub: str | None = None
    auth_type: str
    credentials_set: bool
    ttw_job_done_seconds: float
    is_active: bool
    last_discovered_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProviderServerList(BaseModel):
    servers: list[ProviderServerView]


class MeView(BaseModel):
    sub: str
    display_name: str | None = None
    email: str | None = None
    platform_roles: list[str]
    roles: list[str]
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class RoleVocabulary(BaseModel):
    """Model-access role names of every model ever published (D8, REF-F11)."""

    roles: list[str]


class DiscoveredProcess(BaseModel):
    """A process the server advertises, and whether it is registered yet."""

    id: str
    title: str | None = None
    description: str | None = None
    version: str | None = None
    model_id: UUID | None = None  # set if already registered


class DiscoveryResult(BaseModel):
    server_id: UUID
    processes: list[DiscoveredProcess]


class CreatedModel(BaseModel):
    id: UUID
    slug: str
    remote_process_id: str
    status: str


class ModelsCreated(BaseModel):
    created: list[CreatedModel]
    existing: list[CreatedModel]  # already registered, left unchanged


class LiveInterface(BaseModel):
    """The process description as the server answers right now."""

    sha256: str
    description: dict[str, Any]


class InterfaceView(BaseModel):
    """Drift state of a model plus a live comparison (``GET …/interface``).

    ``diff`` compares the live description with ``verified_interface``; it is
    None when the model was never verified or the server could not be reached
    (``live_error``). Stored fields reflect the last check (``last_checked_at``).
    """

    model_id: UUID
    interface_state: str
    last_checked_at: datetime | None = None
    verified_at: datetime | None = None
    verified_interface_sha256: str | None = None
    verified_interface: dict[str, Any] | None = None
    current_interface_sha256: str | None = None
    live: LiveInterface | None = None
    live_error: str | None = None
    matches_verified: bool | None = None
    diff: dict[str, Any] | None = None


class InterfaceCheckResult(BaseModel):
    model_id: UUID
    slug: str
    remote_process_id: str
    previous_state: str
    interface_state: str
    error: str | None = None


class RediscoveryResult(BaseModel):
    server_id: UUID
    checked: list[InterfaceCheckResult]


class AuditPage(BaseModel):
    events: list[AuditRecord]
    next_cursor: str | None = None


class AccessModelRef(BaseModel):
    id: UUID
    slug: str
    process_id: str  # also the model-access role name, `{provider}:{process}`
    full_name: str | None = None  # in the requested language, else default
    status: str


class AccessUserRef(BaseModel):
    sub: str
    display_name: str | None = None
    email: str | None = None  # access admins only


class AccessRequestView(BaseModel):
    """An access request (REF-F12 phase E1). ``role`` is what UMP-X maps."""

    id: UUID
    model: AccessModelRef
    role: str
    user: AccessUserRef
    status: str
    request_note: str | None = None
    status_note: str | None = None
    status_changed_by: str | None = None
    status_changed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AccessRequestPage(BaseModel):
    requests: list[AccessRequestView]
    next_cursor: str | None = None
