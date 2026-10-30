"""RegistryAccessService — access requests (REF-F12 phase E1, D15).

- ``request``: a user asks for access to a published model.
- ``grant_directly``: an access admin records a grant made from the access
  matrix in UMP-X, without a prior request.
- ``transition``: withdraw (requester), grant / deny (access admin who is not
  the requester), revoke (access admin) — see ``access.TRANSITIONS``.
- ``list_requests`` / ``get_request``: access admins see everything; everyone
  else sees requests about themselves and requests for models they own.

These are records, not grants: the Keycloak role is mapped (or unmapped) by
UMP-X before it records ``granted`` (or ``revoked``) here, and execution keeps
reading roles from the token. Every step is audited in the same transaction.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Callable, Sequence
from uuid import UUID

from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.model_registry import (
    AccessQuery,
    ModelRegistryPort,
    RegistryConflictError,
    StaleWriteError,
)
from ump.core.models.providers_config import canonical_process_id
from ump.core.models.registry import AppUser, RegistryModel, roles
from ump.core.models.registry.access import (
    ACCESS_STATUSES,
    GRANTABLE_MODEL_STATUS,
    AccessRequest,
    AccessTransitionError,
    AccessTransitionForbidden,
    check_transition,
    holds_access,
    is_access_admin,
)
from ump.core.models.registry.audit import AuditEvent
from ump.core.models.registry.card import (
    AccessGrantCreate,
    AccessRequestCreate,
    AccessTransitionRequest,
)
from ump.core.models.registry.localized import pick_language, resolve
from ump.core.models.registry.views import (
    AccessModelRef,
    AccessRequestPage,
    AccessRequestView,
    AccessUserRef,
)
from ump.core.services.authorization import AuthorizationService
from ump.core.services.registry_catalog import RegistryCatalogService
from ump.core.services.registry_common import check_if_match, problem

MAX_PAGE_SIZE = 200


class RegistryAccessService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        catalog: RegistryCatalogService,
        authorization: AuthorizationService,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._authz = authorization
        self._clock = clock

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def request(
        self,
        auth: AuthContext,
        model_id: UUID,
        body: AccessRequestCreate,
        languages: Sequence[str] = (),
    ) -> AccessRequestView:
        if not auth.is_authenticated or not auth.user_id:
            raise problem(401, "Unauthorized", "Authentication required.")
        # 404 unless the caller may see the model at all
        await self._catalog.get_model(auth, model_id)
        model, role, provider = await self._model(model_id)
        if model.status != GRANTABLE_MODEL_STATUS:
            raise problem(
                409, "Conflict", "Access can only be requested for published models."
            )
        if model.process_config.get("anonymous-access"):
            raise problem(
                409, "Conflict", "This model can be run without access rights."
            )
        if holds_access(auth, provider, role):
            raise problem(409, "Conflict", "You already have access to this model.")
        # the requester normally exists already (touch); make sure for the FK
        await self._registry.ensure_user(
            AppUser(sub=auth.user_id, display_name=auth.name, email=auth.email)
        )
        record = AccessRequest(
            user_sub=auth.user_id, model_id=model.id, request_note=body.note
        )
        stored = await self._create(
            record,
            AuditEvent.by(
                auth,
                "access.request",
                model_id=model.id,
                details=self._details(record, role, body.note),
            ),
            "You already have an open access request for this model.",
        )
        return (await self._views(auth, [stored], languages))[0]

    async def grant_directly(
        self,
        auth: AuthContext,
        model_id: UUID,
        body: AccessGrantCreate,
        languages: Sequence[str] = (),
    ) -> AccessRequestView:
        self._authz.require_platform_role(auth, roles.ACCESS_ADMIN)
        model, role, _ = await self._model(model_id)
        if model.status != GRANTABLE_MODEL_STATUS:
            raise problem(
                409, "Conflict", "Access can only be granted to published models."
            )
        if body.user.sub == auth.user_id:
            raise problem(
                403, "Forbidden", "Access admins cannot grant access to themselves."
            )
        await self._registry.ensure_user(
            AppUser(
                sub=body.user.sub, display_name=body.user.name, email=body.user.email
            )
        )
        record = AccessRequest(
            user_sub=body.user.sub,
            model_id=model.id,
            status="granted",
            status_note=body.note,
            status_changed_by=auth.user_id,
        )
        stored = await self._create(
            record,
            AuditEvent.by(
                auth,
                "access.grant",
                model_id=model.id,
                details={**self._details(record, role, body.note), "direct": True},
            ),
            "The user already has an open access record for this model; "
            "decide the pending request instead.",
        )
        return (await self._views(auth, [stored], languages))[0]

    async def transition(
        self,
        auth: AuthContext,
        request_id: UUID,
        body: AccessTransitionRequest,
        if_match: str | None = None,
        languages: Sequence[str] = (),
    ) -> AccessRequestView:
        record = await self._load(auth, request_id)
        if if_match is not None:
            check_if_match(if_match, record.updated_at)
        try:
            step = check_transition(record, body.to_status, auth, body.note)
        except AccessTransitionForbidden as exc:
            raise problem(403, "Forbidden", str(exc))
        except AccessTransitionError as exc:
            title = "Conflict" if exc.status == 409 else "Bad Request"
            raise problem(exc.status, title, str(exc))
        model, role, _ = await self._model(record.model_id)
        if body.to_status == "granted" and model.status != GRANTABLE_MODEL_STATUS:
            raise problem(
                409,
                "Conflict",
                f"The model is '{model.status}'; access can only be granted to "
                "published models. Deny the request with a note instead.",
            )
        updated = record.model_copy(
            update={
                "status": body.to_status,
                "status_note": body.note,
                "status_changed_by": auth.user_id,
                "status_changed_at": self._clock(),
            }
        )
        try:
            stored = await self._registry.update_access_request(
                updated,
                expected_updated_at=record.updated_at,
                audit=AuditEvent.by(
                    auth,
                    step.action,
                    model_id=record.model_id,
                    details={
                        **self._details(record, role, body.note),
                        "from": record.status,
                        "to": body.to_status,
                    },
                ),
            )
        except StaleWriteError:
            raise problem(
                412,
                "Precondition Failed",
                "The access request was changed concurrently; reload and retry.",
            )
        return (await self._views(auth, [stored], languages))[0]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_request(
        self, auth: AuthContext, request_id: UUID, languages: Sequence[str] = ()
    ) -> AccessRequestView:
        record = await self._load(auth, request_id)
        return (await self._views(auth, [record], languages))[0]

    async def list_requests(
        self,
        auth: AuthContext,
        *,
        status: str | None = None,
        model_id: UUID | None = None,
        user: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        languages: Sequence[str] = (),
    ) -> AccessRequestPage:
        if not auth.is_authenticated or not auth.user_id:
            raise problem(401, "Unauthorized", "Authentication required.")
        if status is not None and status not in ACCESS_STATUSES:
            raise problem(400, "Bad Request", f"Unknown status '{status}'.")
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        records = await self._registry.list_access_requests(
            AccessQuery(
                visible_to=None if is_access_admin(auth) else auth.user_id,
                status=status,
                model_id=model_id,
                user_sub=user,
                before=decode_cursor(cursor) if cursor else None,
                limit=limit + 1,  # one more tells whether a next page exists
            )
        )
        page = records[:limit]
        return AccessRequestPage(
            requests=await self._views(auth, page, languages),
            next_cursor=encode_cursor(page[-1]) if len(records) > limit else None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load(self, auth: AuthContext, request_id: UUID) -> AccessRequest:
        """The request, if *auth* may see it; 404 otherwise (no existence leak)."""
        if not auth.is_authenticated or not auth.user_id:
            raise problem(401, "Unauthorized", "Authentication required.")
        record = await self._registry.get_access_request(request_id)
        if record is not None and not is_access_admin(auth):
            if record.user_sub != auth.user_id:
                model = await self._registry.get_model_by_id(record.model_id)
                if model is None or model.owner_sub != auth.user_id:
                    record = None
        if record is None:
            raise problem(404, "Not Found", f"Access request '{request_id}' not found.")
        return record

    async def _model(self, model_id: UUID) -> tuple[RegistryModel, str, str]:
        """The model, its model-access role and its provider name."""
        model = await self._registry.get_model_by_id(model_id)
        if model is None:
            raise problem(404, "Not Found", f"Model '{model_id}' not found.")
        server = await self._registry.get_provider_server_by_id(
            model.provider_server_id
        )
        role = canonical_process_id(server.name, model.remote_process_id)
        return model, role, server.name

    async def _create(
        self, record: AccessRequest, audit: AuditEvent, conflict: str
    ) -> AccessRequest:
        try:
            return await self._registry.create_access_request(record, audit=audit)
        except RegistryConflictError:
            raise problem(409, "Conflict", conflict)

    @staticmethod
    def _details(record: AccessRequest, role: str, note: str | None) -> dict:
        return {
            "access_request_id": str(record.id),
            "user_sub": record.user_sub,
            "role": role,
            "note": note,
        }

    async def _views(
        self,
        auth: AuthContext,
        records: list[AccessRequest],
        languages: Sequence[str],
    ) -> list[AccessRequestView]:
        models: dict[UUID, tuple[RegistryModel, str]] = {}
        for model_id in {r.model_id for r in records}:
            model, role, _ = await self._model(model_id)
            models[model_id] = (model, role)
        users = await self._registry.get_users({r.user_sub for r in records})
        show_email = is_access_admin(auth)

        def view(record: AccessRequest) -> AccessRequestView:
            model, role = models[record.model_id]
            lang = pick_language(languages, list(model.full_name), model.default_lang)
            user = users.get(record.user_sub)
            return AccessRequestView(
                id=record.id,
                model=AccessModelRef(
                    id=model.id,
                    slug=model.slug,
                    process_id=role,
                    full_name=resolve(model.full_name, lang, model.default_lang),
                    status=model.status,
                ),
                role=role,
                user=AccessUserRef(
                    sub=record.user_sub,
                    display_name=user.display_name if user else None,
                    email=user.email if user and show_email else None,
                ),
                status=record.status,
                request_note=record.request_note,
                status_note=record.status_note,
                status_changed_by=record.status_changed_by,
                status_changed_at=record.status_changed_at,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )

        return [view(r) for r in records]


def encode_cursor(record: AccessRequest) -> str:
    raw = f"{record.created_at.isoformat()}|{record.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode()
        created, _, rid = raw.partition("|")
        return datetime.fromisoformat(created), UUID(rid)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise problem(400, "Bad Request", "Invalid cursor.")
