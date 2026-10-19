"""Model-card shapes and write inputs (REF-F12 "Data model", D13).

The database only checks the JSON type of the JSONB columns; their content is
validated here, before anything reaches a repository. Language maps ("L") may
only use the platform languages (``UMP_REGISTRY_LANGUAGES``) and never hold
empty strings.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from ump.core.models.providers_config import (
    ApiKeyAuthConfig,
    BasicAuthConfig,
    BearerTokenAuthConfig,
    ProcessConfig,
)

SPDX_PATTERN = re.compile(r"^[A-Za-z0-9.+:()\- ]+$")  # same as CHECK ck_model_license
LANG_PATTERN = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")  # same as ck_model_default_lang
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

LocalizedText = dict[str, str]
LocalizedList = dict[str, list[str]]


class CardError(ValueError):
    """A model card or server input violates a registry rule."""


def check_localized(
    value: dict[str, Any], languages: frozenset[str], field: str, *, lists=False
) -> None:
    for lang, text in value.items():
        if lang not in languages:
            raise CardError(
                f"{field}: language '{lang}' is not one of {sorted(languages)}"
            )
        items = text if lists else [text]
        if lists and not isinstance(text, list):
            raise CardError(f"{field}.{lang}: expected a list of strings")
        if any(not isinstance(t, str) or not t.strip() for t in items):
            raise CardError(f"{field}.{lang}: empty text is not allowed")


# ---------------------------------------------------------------------------
# JSONB shapes
# ---------------------------------------------------------------------------


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Requirements(_Shape):
    cpu_cores: float | None = None
    memory_gb: float | None = None
    gpu: str | bool | None = None
    storage_gb: float | None = None
    runtime_seconds: float | None = None
    software: list[str] | None = None


class Contributor(_Shape):
    name: str = Field(min_length=1)
    role: Literal["author", "developer", "contributor", "maintainer", "funder"]
    orcid: str | None = None
    affiliation: str | None = None
    email: str | None = None


class LiteratureRef(_Shape):
    citation: str = Field(min_length=1)
    doi: str | None = None
    url: HttpUrl | None = None
    year: int | None = None


class Application(_Shape):
    title: LocalizedText
    description: LocalizedText | None = None
    location: str | None = None
    url: HttpUrl | None = None
    year: int | None = None


class TestCase(_Shape):
    __test__ = False  # not a pytest class

    name: str = Field(min_length=1)
    description: LocalizedText | None = None
    input_url: HttpUrl
    expected_output_url: HttpUrl
    documentation_url: HttpUrl | None = None
    input_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN.pattern)
    output_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN.pattern)


class ProcessSettings(ProcessConfig):
    """The providers.yaml process entry without what the registry owns itself."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str = "_"  # the registry's remote_process_id is authoritative


_PROCESS_SETTINGS_OWNED = {"id", "description", "version", "state", "exclude"}


def process_settings_to_json(settings: ProcessSettings) -> dict[str, Any]:
    return settings.model_dump(
        mode="json", by_alias=True, exclude=_PROCESS_SETTINGS_OWNED, exclude_none=True
    )


# ---------------------------------------------------------------------------
# Write inputs (request bodies of the registry API)
# ---------------------------------------------------------------------------

Credentials = BasicAuthConfig | ApiKeyAuthConfig | BearerTokenAuthConfig


class ServerCreate(_Shape):
    name: str
    base_url: HttpUrl
    # absent = NoAuth; the secret is encrypted and never returned
    authentication: Credentials | None = None
    ttw_job_done_seconds: float = Field(default=1500.0, gt=0)


class ServerPatch(_Shape):
    """Every field optional; ``name`` is immutable and therefore not accepted.

    ``authentication``: an auth config replaces type and secret; ``null``
    switches the server to NoAuth and deletes the stored secret.
    """

    base_url: HttpUrl | None = None
    authentication: Credentials | None = None
    ttw_job_done_seconds: float | None = Field(default=None, gt=0)
    is_active: bool | None = None


class ModelsCreate(_Shape):
    remote_process_ids: list[str] = Field(min_length=1, max_length=100)
    default_lang: str | None = None  # default: the first platform language

    @field_validator("remote_process_ids")
    @classmethod
    def _unique(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids) or any(not i for i in ids):
            raise ValueError("remote_process_ids must be unique and non-empty")
        return ids


class ModelCardPatch(_Shape):
    """Card fields of PATCH /models/{id}. A field present replaces the stored
    value as a whole (a language map is replaced, not merged)."""

    default_lang: str | None = None
    full_name: LocalizedText | None = None
    short_description: LocalizedText | None = None
    purpose: LocalizedText | None = None
    limitations_risks: LocalizedText | None = None
    keywords: LocalizedList | None = None
    version_label: str | None = None
    repository_url: HttpUrl | None = None
    code_ref: str | None = None
    license: str | None = None
    requirements: Requirements | None = None
    contributors: list[Contributor] | None = None
    literature: list[LiteratureRef] | None = None
    applications: list[Application] | None = None
    test_cases: list[TestCase] | None = None
    io_notes: dict[str, LocalizedText] | None = None
    process_config: ProcessSettings | None = None

    def check_languages(self, languages: frozenset[str]) -> None:
        for field in ("full_name", "short_description", "purpose", "limitations_risks"):
            value = getattr(self, field)
            if value is not None:
                check_localized(value, languages, field)
        if self.keywords is not None:
            check_localized(self.keywords, languages, "keywords", lists=True)
        for key, note in (self.io_notes or {}).items():
            check_localized(note, languages, f"io_notes.{key}")
        for i, app in enumerate(self.applications or []):
            check_localized(app.title, languages, f"applications[{i}].title")
            if app.description:
                check_localized(
                    app.description, languages, f"applications[{i}].description"
                )
        for i, case in enumerate(self.test_cases or []):
            if case.description:
                check_localized(
                    case.description, languages, f"test_cases[{i}].description"
                )
        if self.license is not None and not SPDX_PATTERN.match(self.license):
            raise CardError("license: not an SPDX license expression")
        if self.default_lang is not None and self.default_lang not in languages:
            raise CardError(f"default_lang must be one of {sorted(languages)}")


class TransitionRequest(_Shape):
    to_status: str
    note: str | None = Field(default=None, max_length=4000)
    verification_job_id: str | None = None  # required for 'verified'


class ReverifyRequest(_Shape):
    verification_job_id: str
    note: str | None = Field(default=None, max_length=4000)


class OwnerTransfer(_Shape):
    owner_sub: str = Field(min_length=1)
