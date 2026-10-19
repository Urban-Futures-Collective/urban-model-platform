"""RegistryVerificationService — review workflow (REF-F12 phase C).

- ``transition``: one step of the status graph (``workflow.TRANSITIONS``).
- ``reverify``: a verifier re-verifies a ``verified``/``published`` model in place,
  e.g. after interface drift; the status does not change.
- ``transfer_owner``: a platform admin hands a model to another account.

Verification is evidence-based. The verifier names a job of theirs, and UMP
checks it in its own job store: the job exists, the verifier started it, it ran
this model, it finished ``successful``, and it was started after the current
review round began (after the last verification for ``reverify``). Then the
current raw process description is fetched and stored as the verified
interface, together with its hash.

Every change is audited; steps that can change what the OGC endpoints see
reload the provider snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.credential_cipher import CredentialCipherPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.model_registry import ModelRegistryPort, StaleWriteError
from ump.core.interfaces.providers import ProvidersPort
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.interfaces.safe_fetch import SafeFetchPort
from ump.core.models.providers_config import canonical_process_id
from ump.core.models.registry import (
    AppUser,
    ProviderServer,
    RegistryModel,
    interface_sha256,
    roles,
)
from ump.core.models.registry.audit import AuditEvent
from ump.core.models.registry.card import (
    OwnerTransfer,
    ReverifyRequest,
    TransitionRequest,
)
from ump.core.models.registry.views import ModelCardView
from ump.core.models.registry.workflow import (
    TransitionError,
    TransitionForbidden,
    check_transition,
    may_act,
)
from ump.core.services.authorization import AuthorizationService
from ump.core.services.registry_catalog import RegistryCatalogService
from ump.core.services.registry_common import check_if_match, problem
from ump.core.services.registry_remote import RegistryRemote


class RegistryVerificationService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        jobs: JobRepositoryPort,
        catalog: RegistryCatalogService,
        authorization: AuthorizationService,
        providers: ProvidersPort,
        fetch: SafeFetchPort,
        remote_auth: RemoteAuthPort,
        cipher: CredentialCipherPort | None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._registry = registry
        self._jobs = jobs
        self._catalog = catalog
        self._authz = authorization
        self._providers = providers
        self._remote = RegistryRemote(fetch, remote_auth, cipher)
        self._clock = clock

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    async def transition(
        self,
        auth: AuthContext,
        model_id: UUID,
        body: TransitionRequest,
        if_match: str | None = None,
    ) -> ModelCardView:
        model, server = await self._load(auth, model_id, if_match)
        try:
            check_transition(
                model, body.to_status, auth, body.note, body.verification_job_id
            )
        except TransitionForbidden as exc:
            raise problem(403, "Forbidden", str(exc))
        except TransitionError as exc:
            title = "Conflict" if exc.status == 409 else "Bad Request"
            raise problem(exc.status, title, str(exc))

        now = self._clock()
        changes: dict[str, Any] = {
            "status": body.to_status,
            "status_note": body.note,
            "status_changed_by": auth.user_id,
            "status_changed_at": now,
        }
        if body.to_status == "verified":
            await self._check_job(
                auth,
                model,
                server,
                body.verification_job_id,
                since=model.status_changed_at,
            )
            changes.update(
                await self._verification(auth, server, model, body.verification_job_id)
            )
        if body.to_status == "published" and model.published_at is None:
            # first publication: the model-access roles exist from now on (D8)
            changes["published_at"] = now

        updated = model.model_copy(update=changes)
        await self._store(
            model,
            updated,
            AuditEvent.by(
                auth,
                "model.status.change",
                model_id=model.id,
                details={
                    "from": model.status,
                    "to": body.to_status,
                    "note": body.note,
                    "verification_job_id": body.verification_job_id,
                },
            ),
        )
        if model.exec_state != updated.exec_state:
            self._providers.load_providers()  # what /v1.0 may run changes now
        return await self._catalog.get_model(auth, model_id, ["*"])

    async def reverify(
        self,
        auth: AuthContext,
        model_id: UUID,
        body: ReverifyRequest,
        if_match: str | None = None,
    ) -> ModelCardView:
        model, server = await self._load(auth, model_id, if_match)
        if model.status not in ("verified", "published"):
            raise problem(
                409, "Conflict", "Only verified or published models can be re-verified."
            )
        if not may_act("reviewer", model, auth):
            raise problem(
                403,
                "Forbidden",
                "Re-verification is for a verifier who is not the owner.",
            )
        await self._check_job(
            auth, model, server, body.verification_job_id, since=model.verified_at
        )
        changes = await self._verification(
            auth, server, model, body.verification_job_id
        )
        if body.note:
            changes["status_note"] = body.note
        updated = model.model_copy(update=changes)
        await self._store(
            model,
            updated,
            AuditEvent.by(
                auth,
                "model.reverify",
                model_id=model.id,
                details={
                    "verification_job_id": body.verification_job_id,
                    "previous_sha256": model.verified_interface_sha256,
                    "sha256": updated.verified_interface_sha256,
                    "note": body.note,
                },
            ),
        )
        return await self._catalog.get_model(auth, model_id, ["*"])

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    async def transfer_owner(
        self,
        auth: AuthContext,
        model_id: UUID,
        body: OwnerTransfer,
        if_match: str | None,
    ) -> ModelCardView:
        self._authz.require_platform_role(auth, roles.PLATFORM_ADMIN)
        model = await self._registry.get_model_by_id(model_id)
        if model is None:
            raise problem(404, "Not Found", f"Model '{model_id}' not found.")
        check_if_match(if_match, model.updated_at)
        if body.owner_sub == model.owner_sub:
            return await self._catalog.get_model(auth, model_id, ["*"])
        if model.verified_by == body.owner_sub:
            raise problem(
                409,
                "Conflict",
                "The new owner verified this model; an owner never verifies their "
                "own model (four-eyes). Re-verify by someone else first.",
            )
        await self._registry.upsert_user(AppUser(sub=body.owner_sub))  # FK owner
        await self._store(
            model,
            model.model_copy(update={"owner_sub": body.owner_sub}),
            AuditEvent.by(
                auth,
                "model.owner.transfer",
                model_id=model.id,
                details={"before": model.owner_sub, "after": body.owner_sub},
            ),
        )
        return await self._catalog.get_model(auth, model_id, ["*"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load(
        self, auth: AuthContext, model_id: UUID, if_match: str | None
    ) -> tuple[RegistryModel, ProviderServer]:
        if not auth.is_authenticated:
            raise problem(401, "Unauthorized", "Authentication required.")
        # 404 unless the caller may see the model at all
        await self._catalog.get_model(auth, model_id)
        model = await self._registry.get_model_by_id(model_id)
        if model is None:
            raise problem(404, "Not Found", f"Model '{model_id}' not found.")
        if if_match is not None:
            check_if_match(if_match, model.updated_at)
        server = await self._registry.get_provider_server_by_id(
            model.provider_server_id
        )
        return model, server

    async def _check_job(
        self,
        auth: AuthContext,
        model: RegistryModel,
        server: ProviderServer,
        job_id: str | None,
        since: datetime | None,
    ) -> None:
        def reject(why: str):
            return problem(400, "Bad Request", f"verification_job_id: {why}")

        job = await self._jobs.get(job_id) if job_id else None
        if job is None:
            raise reject(f"no job '{job_id}'.")
        if job.user_id != auth.user_id:
            raise reject("the job was not started by you.")
        canonical = canonical_process_id(server.name, model.remote_process_id)
        if job.provider != server.name or job.process_id not in (
            canonical,
            model.remote_process_id,
        ):
            raise reject(f"the job did not run '{canonical}'.")
        status = job.status or (job.status_info.status if job.status_info else None)
        if str(status) != "successful":
            raise reject(f"the job is '{status}', not 'successful'.")
        if since is not None and job.created < since:
            raise reject("the job ran before the current review round began.")

    async def _verification(
        self,
        auth: AuthContext,
        server: ProviderServer,
        model: RegistryModel,
        job_id: str | None,
    ) -> dict[str, Any]:
        """The verified_* fields: the interface as it is right now, raw.

        Fetched in the model's default language, like the draft, because the
        interface hash is defined on the default-language response (REF-F12).
        """
        description = await self._remote.get(
            server, f"processes/{model.remote_process_id}", lang=model.default_lang
        )
        if not isinstance(description, dict):
            raise problem(
                502, "Bad Gateway", "Process description is not a JSON object."
            )
        sha = interface_sha256(description)
        now = self._clock()
        return {
            "verified_by": auth.user_id,
            "verified_at": now,
            "verification_job_id": job_id,
            "verified_interface": description,
            "verified_interface_sha256": sha,
            "current_interface_sha256": sha,
            "interface_state": "in_sync",
            "last_checked_at": now,
        }

    async def _store(
        self, loaded: RegistryModel, updated: RegistryModel, audit: AuditEvent
    ) -> RegistryModel:
        try:
            return await self._registry.update_model(
                updated, expected_updated_at=loaded.updated_at, audit=audit
            )
        except StaleWriteError:
            raise problem(
                412,
                "Precondition Failed",
                "The model was changed concurrently; reload and retry.",
            )
