"""Read models the registry API returns (REF-F12 "API sketch").

Plain Pydantic in the core, like ``ToolCatalog`` for the MCP router: the web
adapter serialises them as they are. Credentials never appear in any view.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


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
