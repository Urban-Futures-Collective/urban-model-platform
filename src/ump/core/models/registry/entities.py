"""Registry entities mirroring the tables of migration 0003 (schema `registry`).

Only the fields used so far are modelled; the remaining model-card columns
(contributors, literature, …) keep their database defaults until the
submission workflow (REF-F12 phase 3) needs them.
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


def slugify(value: str) -> str:
    """Lower-case ``value`` and collapse everything but [a-z0-9] into '-'."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


class AppUser(BaseModel):
    sub: str
    display_name: str | None = None
    email: str | None = None


class ProviderServer(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    base_url: str
    owner_sub: str | None = None
    auth_type: AuthType = "NoAuth"
    credentials: bytes | None = None  # ciphertext, see CredentialCipherPort
    ttw_job_done_seconds: float = 1500.0
    is_active: bool = True


class RegistryModel(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    slug: str
    provider_server_id: UUID
    remote_process_id: str
    owner_sub: str
    default_lang: str = "en"
    full_name: dict[str, str]
    short_description: dict[str, str] = Field(default_factory=dict)
    purpose: dict[str, str] = Field(default_factory=dict)
    keywords: dict[str, list[str]] = Field(default_factory=dict)
    version_label: str | None = None
    license: str | None = None
    process_config: dict[str, Any] = Field(default_factory=dict)
    status: ModelStatus = "draft"
    status_changed_by: str | None = None
    status_changed_at: datetime | None = None
    published_at: datetime | None = None
    verified_by: str | None = None
    verified_at: datetime | None = None
    verified_interface: dict[str, Any] | None = None
    verified_interface_sha256: str | None = None
    current_interface_sha256: str | None = None
    interface_state: InterfaceState = "unchecked"
    last_checked_at: datetime | None = None
