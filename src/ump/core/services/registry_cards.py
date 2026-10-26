"""RegistryCardService — the owner edits a model card (REF-F12 phase B).

Rules:
- only the owner (``provider`` role) edits a card; anybody else gets 403 if the
  model is visible to them, 404 otherwise;
- no edits while ``deactivated``;
- a field sent replaces the stored value as a whole;
- the database invariants are checked here first, with readable errors:
  ``full_name`` contains ``default_lang``; beyond ``draft``/``submitted`` the card
  keeps ``purpose`` in ``default_lang`` and a ``license``;
- ``process_config`` (execution settings such as ``anonymous-access``) changes
  who may run the model, so the owner may change it only while the model is
  not under review or live (``draft``, ``changes_requested``);
- card edits do not change the status (D12) and are audited with a
  before/after diff.
"""

from __future__ import annotations

from typing import Any, Sequence
from uuid import UUID

from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.model_registry import ModelRegistryPort, StaleWriteError
from ump.core.interfaces.providers import ProvidersPort
from ump.core.models.registry import RegistryModel, roles
from ump.core.models.registry.audit import AuditEvent, diff
from ump.core.models.registry.card import (
    CardError,
    ModelCardPatch,
    process_settings_to_json,
)
from ump.core.models.registry.views import ModelCardView
from ump.core.services.authorization import AuthorizationService
from ump.core.services.registry_catalog import RegistryCatalogService
from ump.core.services.registry_common import check_if_match, problem

PROCESS_CONFIG_EDITABLE = frozenset({"draft", "changes_requested"})
CARD_MUST_BE_COMPLETE = frozenset(
    {
        "in_review",
        "changes_requested",
        "verified",
        "published",
        "rejected",
        "deactivated",
    }
)


class RegistryCardService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        catalog: RegistryCatalogService,
        authorization: AuthorizationService,
        providers: ProvidersPort,
        languages: Sequence[str],
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._authz = authorization
        self._providers = providers
        self._languages = frozenset(languages)

    async def update_card(
        self,
        auth: AuthContext,
        model_id: UUID,
        patch: ModelCardPatch,
        if_match: str | None,
    ) -> ModelCardView:
        self._authz.require_platform_role(auth, roles.PROVIDER)
        model = await self._registry.get_model_by_id(model_id)
        if model is None or model.owner_sub != auth.user_id:
            # 403 only if the caller may see the model at all (raises 404 if not)
            await self._catalog.get_model(auth, model_id)
            raise problem(403, "Forbidden", "Only the owner edits a model card.")
        if model.status == "deactivated":
            raise problem(409, "Conflict", "A deactivated model cannot be edited.")
        check_if_match(if_match, model.updated_at)

        try:
            patch.check_languages(self._languages)
        except CardError as exc:
            raise problem(400, "Bad Request", str(exc))

        changes = self._changes(model, patch)
        if not changes:
            return await self._catalog.get_model(auth, model_id, ["*"])
        updated = model.model_copy(update=changes)
        self._check_invariants(updated)

        before = model.model_dump(mode="json", include=set(changes))
        after = updated.model_dump(mode="json", include=set(changes))
        try:
            await self._registry.update_model(
                updated,
                expected_updated_at=model.updated_at,
                audit=AuditEvent.by(
                    auth,
                    "model.card.update",
                    model_id=model.id,
                    details={"changes": diff(before, after)},
                ),
            )
        except StaleWriteError:
            raise problem(
                412,
                "Precondition Failed",
                "The model was changed concurrently; reload and retry.",
            )
        if updated.exec_state is not None:
            # name, version and process settings are part of the provider snapshot
            self._providers.load_providers()
        return await self._catalog.get_model(auth, model_id, ["*"])

    def _changes(self, model: RegistryModel, patch: ModelCardPatch) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        for field in patch.model_fields_set:
            value = getattr(patch, field)
            if field == "process_config":
                if model.status not in PROCESS_CONFIG_EDITABLE:
                    raise problem(
                        409,
                        "Conflict",
                        "Execution settings (process_config) change who may run the "
                        "model; they can be edited only in status draft or "
                        "changes_requested.",
                    )
                value = process_settings_to_json(value) if value is not None else {}
            elif field == "repository_url":
                value = str(value) if value is not None else None
            elif field in ("requirements",):
                value = (
                    value.model_dump(mode="json", exclude_none=True) if value else {}
                )
            elif field in ("contributors", "literature", "applications", "test_cases"):
                value = [
                    item.model_dump(mode="json", exclude_none=True)
                    for item in (value or [])
                ]
            elif value is None and field in (
                "short_description",
                "purpose",
                "limitations_risks",
                "keywords",
                "io_notes",
            ):
                value = {}
            elif value is None and field in ("full_name", "default_lang"):
                raise problem(400, "Bad Request", f"{field} cannot be removed.")
            if getattr(model, field) != value:
                changes[field] = value
        return changes

    @staticmethod
    def _check_invariants(model: RegistryModel) -> None:
        lang = model.default_lang
        if lang not in model.full_name:
            raise problem(
                400,
                "Bad Request",
                f"full_name must contain the default language '{lang}'.",
            )
        if model.status in CARD_MUST_BE_COMPLETE:
            if lang not in model.purpose:
                raise problem(
                    400,
                    "Bad Request",
                    f"purpose in '{lang}' is required once a model is in review.",
                )
            if not model.license:
                raise problem(
                    400, "Bad Request", "license is required once a model is in review."
                )
