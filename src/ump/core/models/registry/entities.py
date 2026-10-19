"""Registry entities mirroring the tables of migration 0003 (schema `registry`).

Fields set by the database (timestamps) are None until the row is stored.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

# Slug / provider-name rule, identical to the CHECK constraints in 0003.
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


ModelStatus = Literal[
    "draft",
    "submitted",
    "in_review",
    "changes_requested",
    "verified",
    "published",
    "rejected",
    "deactivated",
]
InterfaceState = Literal["unchecked", "in_sync", "drifted", "unreachable"]
AuthType = Literal["NoAuth", "BasicAuth", "ApiKey", "BearerToken"]


def exec_state_for(status: str) -> str | None:
    """What the execution path sees for a model status (D7, D9).

    Mirrors the generated column ``registry.model.exec_state`` (migration 0003).
    """
    return {
        "published": "published",
        "in_review": "in_review",
        "verified": "in_review",
        "changes_requested": "deactivated",
        "rejected": "deactivated",
        "deactivated": "deactivated",
    }.get(status)


def slugify(value: str) -> str:
    """Lower-case ``value`` and collapse everything but [a-z0-9] into '-'."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


class AppUser(BaseModel):
    sub: str
    display_name: str | None = None
    email: str | None = None
    # set by the database; None until stored
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class ProviderServer(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    base_url: str
    owner_sub: str | None = None
    auth_type: AuthType = "NoAuth"
    credentials: bytes | None = None  # ciphertext, see CredentialCipherPort
    ttw_job_done_seconds: float = 1500.0
    is_active: bool = True
    last_discovered_at: datetime | None = None
    # set by the database; None until stored
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RegistryModel(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    slug: str
    provider_server_id: UUID
    remote_process_id: str
    owner_sub: str
    default_lang: str = "de"
    full_name: dict[str, str]
    short_description: dict[str, str] = Field(default_factory=dict)
    purpose: dict[str, str] = Field(default_factory=dict)
    limitations_risks: dict[str, str] = Field(default_factory=dict)
    keywords: dict[str, list[str]] = Field(default_factory=dict)
    version_label: str | None = None
    repository_url: str | None = None
    code_ref: str | None = None
    license: str | None = None
    # descriptive JSONB parts; their shapes are validated once card editing
    # (REF-F12 phase B) writes them
    requirements: dict[str, Any] = Field(default_factory=dict)
    contributors: list[dict[str, Any]] = Field(default_factory=list)
    literature: list[dict[str, Any]] = Field(default_factory=list)
    applications: list[dict[str, Any]] = Field(default_factory=list)
    test_cases: list[dict[str, Any]] = Field(default_factory=list)
    io_notes: dict[str, dict[str, str]] = Field(default_factory=dict)
    process_config: dict[str, Any] = Field(default_factory=dict)
    status: ModelStatus = "draft"
    status_note: str | None = None
    status_changed_by: str | None = None
    status_changed_at: datetime | None = None
    published_at: datetime | None = None
    verified_by: str | None = None
    verified_at: datetime | None = None
    verification_job_id: str | None = None
    verified_interface: dict[str, Any] | None = None
    verified_interface_sha256: str | None = None
    current_interface_sha256: str | None = None
    interface_state: InterfaceState = "unchecked"
    last_checked_at: datetime | None = None
    # set by the database; None until stored
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def exec_state(self) -> str | None:
        return exec_state_for(self.status)

    def language_maps(self) -> list[dict[str, Any]]:
        """Every free-text language map of the card (for 'languages')."""
        maps: list[dict[str, Any]] = [
            self.full_name,
            self.short_description,
            self.purpose,
            self.limitations_risks,
            self.keywords,
        ]
        maps.extend(self.io_notes.values())
        return maps
