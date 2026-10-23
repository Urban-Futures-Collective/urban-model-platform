"""RegistryCatalogService — read access to model cards (REF-F12 phase A).

Visibility (D10 and "Authorization"):

- Browsing needs ``UMP_PUBLIC_PROCESSES=true`` or the ``viewer``/``user`` role;
  an anonymous caller on a non-public catalogue gets 401.
- Everyone who may browse sees ``published`` models.
- Verifiers additionally see every status except ``draft`` (their review queue);
  platform admins see everything.
- Owners always see their own models, in any status.
- A model outside the caller's scope is reported as 404, so its existence does
  not leak.

Language-map fields are resolved to one language (see ``localized``), or
returned whole for ``lang='*'``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Sequence
from uuid import UUID

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.model_registry import ModelQuery, ModelRegistryPort
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.providers_config import canonical_process_id
from ump.core.models.registry import RegistryModel, roles
from ump.core.models.registry.localized import pick_language, resolve
from ump.core.models.registry.views import (
    ModelCardView,
    ModelPage,
    ReviewInfo,
    RoleVocabulary,
)
from ump.core.services.authorization import AuthorizationService

ALL_LANGUAGES = "*"
MAX_PAGE_SIZE = 200
ALL_STATUSES = frozenset(
    {
        "draft",
        "submitted",
        "in_review",
        "changes_requested",
        "verified",
        "published",
        "rejected",
        "deactivated",
    }
)


def _problem(status: int, title: str, detail: str) -> OGCProcessException:
    return OGCProcessException(
        OGCExceptionResponse(
            type="about:blank", title=title, status=status, detail=detail
        )
    )


def encode_cursor(slug: str) -> str:
    return base64.urlsafe_b64encode(slug.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        slug = base64.b64decode(padded, altchars=b"-_", validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        slug = ""
    if not slug:
        raise _problem(400, "Bad Request", "Invalid cursor.")
    return slug


class RegistryCatalogService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        authorization: AuthorizationService,
        public_catalogue: bool,
    ) -> None:
        self._registry = registry
        self._authz = authorization
        self._public = public_catalogue

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------

    def _visible_statuses(self, auth: AuthContext) -> frozenset[str]:
        """Statuses the caller sees on models they do not own."""
        held = set(auth.roles) if auth.is_authenticated else set()
        if roles.PLATFORM_ADMIN in held:
            return ALL_STATUSES
        if roles.VERIFIER in held:
            return ALL_STATUSES - {"draft"}
        if self._public or held & roles.CATALOGUE_ROLES:
            return frozenset({"published"})
        if not auth.is_authenticated:
            raise _problem(401, "Unauthorized", "Authentication required.")
        return frozenset()  # authenticated, no catalogue role: own models only

    def _sees_review(self, auth: AuthContext, model: RegistryModel) -> bool:
        if not auth.is_authenticated:
            return False
        held = set(auth.roles)
        return (
            model.owner_sub == auth.user_id
            or roles.VERIFIER in held
            or roles.PLATFORM_ADMIN in held
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_models(
        self,
        auth: AuthContext,
        languages: Sequence[str] = (),
        status: str | None = None,
        provider: str | None = None,
        mine: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ModelPage:
        if mine and not auth.is_authenticated:
            raise _problem(401, "Unauthorized", "Authentication required.")
        if status is not None and status not in ALL_STATUSES:
            raise _problem(400, "Bad Request", f"Unknown status '{status}'.")
        visible = self._visible_statuses(auth)

        server_id = None
        if provider is not None:
            server = await self._registry.get_provider_server(provider)
            if server is None:
                return ModelPage(models=[])
            server_id = server.id

        limit = max(1, min(limit, MAX_PAGE_SIZE))
        rows = await self._registry.list_models(
            ModelQuery(
                visible_statuses=None if visible == ALL_STATUSES else visible,
                visible_owner=auth.user_id if auth.is_authenticated else None,
                status=status,
                owner_sub=auth.user_id if mine else None,
                provider_server_id=server_id,
                after_slug=decode_cursor(cursor) if cursor else None,
                limit=limit + 1,  # one extra row tells whether a next page exists
            )
        )
        next_cursor = encode_cursor(rows[limit - 1].slug) if len(rows) > limit else None
        rows = rows[:limit]
        servers = await self._registry.get_provider_servers_by_ids(
            {m.provider_server_id for m in rows}
        )
        return ModelPage(
            models=[
                self._view(m, servers[m.provider_server_id].name, auth, languages)
                for m in rows
            ],
            next_cursor=next_cursor,
        )

    async def get_model(
        self, auth: AuthContext, model_id: UUID, languages: Sequence[str] = ()
    ) -> ModelCardView:
        statuses = self._visible_statuses(auth)
        model = await self._registry.get_model_by_id(model_id)
        owns = (
            model is not None
            and auth.is_authenticated
            and model.owner_sub == auth.user_id
        )
        if model is None or not (owns or model.status in statuses):
            raise _problem(404, "Not Found", f"Model '{model_id}' not found.")
        server = await self._registry.get_provider_server_by_id(
            model.provider_server_id
        )
        return self._view(model, server.name, auth, languages)

    async def role_vocabulary(self, auth: AuthContext) -> RoleVocabulary:
        self._authz.require_platform_role(auth, roles.ACCESS_ADMIN, roles.VERIFIER)
        published = await self._registry.list_published_process_ids()
        return RoleVocabulary(roles=roles.model_access_roles(published))

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------

    def _view(
        self,
        model: RegistryModel,
        server_name: str,
        auth: AuthContext,
        preferences: Sequence[str],
    ) -> ModelCardView:
        available = sorted(
            {lang for m in model.language_maps() for lang in (m or {})}
            | {model.default_lang}
        )
        whole = ALL_LANGUAGES in preferences
        lang = (
            ALL_LANGUAGES
            if whole
            else pick_language(preferences, available, model.default_lang)
        )

        def text(value: Any) -> Any:
            if whole:
                return value or None
            return resolve(value, lang, model.default_lang)

        def nested(items: list[dict[str, Any]], keys: tuple[str, ...]) -> list:
            return [
                {k: (text(v) if k in keys else v) for k, v in item.items()}
                for item in items
            ]

        review = (
            ReviewInfo(
                status_note=model.status_note,
                status_changed_by=model.status_changed_by,
                status_changed_at=model.status_changed_at,
                verified_by=model.verified_by,
                verified_at=model.verified_at,
                verification_job_id=model.verification_job_id,
            )
            if self._sees_review(auth, model)
            else None
        )
        return ModelCardView(
            id=model.id,
            slug=model.slug,
            process_id=canonical_process_id(server_name, model.remote_process_id),
            provider=server_name,
            remote_process_id=model.remote_process_id,
            owner_sub=model.owner_sub,
            lang=lang,
            languages=available,
            default_lang=model.default_lang,
            full_name=text(model.full_name),
            short_description=text(model.short_description),
            purpose=text(model.purpose),
            limitations_risks=text(model.limitations_risks),
            keywords=text(model.keywords),
            version_label=model.version_label,
            repository_url=model.repository_url,
            code_ref=model.code_ref,
            license=model.license,
            requirements=model.requirements,
            contributors=model.contributors,
            literature=model.literature,
            applications=nested(model.applications, ("title", "description")),
            test_cases=nested(model.test_cases, ("description",)),
            io_notes={k: text(v) for k, v in model.io_notes.items()},
            status=model.status,
            exec_state=model.exec_state,
            interface_state=model.interface_state,
            published_at=model.published_at,
            updated_at=model.updated_at,
            review=review,
        )
