"""InterfaceDriftService — interface drift, policy A "flag only" (REF-F12 D5).

The provider controls the model server, so UMP can only *detect* interface
changes. A check fetches the raw description of every model that the execution
path knows (non-NULL ``exec_state``), in the model's default language, through
``SafeFetchPort`` (so an edited or re-pointed ``base_url`` is checked again),
and compares its interface hash:

| outcome                                  | ``interface_state`` |
|------------------------------------------|---------------------|
| equals ``verified_interface_sha256``     | ``in_sync``         |
| differs from it                          | ``drifted``         |
| never verified                           | ``in_sync``         |
| unreachable / unsafe / not a JSON object | ``unreachable``     |

Audit events are written only when something changes: ``model.interface.drifted``
(with a diff summary; again if a drifted model drifts further),
``model.interface.restored``, ``model.interface.unreachable``, and
``model.interface.changed`` for a model that has no verified interface yet.

Execution is never blocked (policy A). Results are stored without touching
``updated_at``, and only if no re-verification landed meanwhile (see
``InterfaceCheck``).

Runs periodically (``InterfaceCheckScheduler``) and on demand —
``POST /servers/{id}/rediscover``, the "I updated my server" button.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.credential_cipher import CredentialCipherPort
from ump.core.interfaces.model_registry import (
    InterfaceCheck,
    ModelQuery,
    ModelRegistryPort,
)
from ump.core.interfaces.poll_lock import PollLockPort
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.interfaces.safe_fetch import SafeFetchPort
from ump.core.models.registry import (
    ProviderServer,
    RegistryModel,
    exec_state_for,
    interface_sha256,
    roles,
)
from ump.core.models.registry.audit import AuditEvent
from ump.core.models.registry.interface_diff import interface_diff
from ump.core.models.registry.views import (
    InterfaceCheckResult,
    InterfaceView,
    LiveInterface,
    RediscoveryResult,
)
from ump.core.services.authorization import AuthorizationService
from ump.core.services.registry_catalog import ALL_STATUSES, RegistryCatalogService
from ump.core.services.registry_common import owns_server, problem
from ump.core.services.registry_remote import RegistryRemote

logger = logging.getLogger(__name__)

# PollLockPort keys are job UUIDs; the periodic check uses a fixed one.
INTERFACE_CHECK_LOCK_KEY = str(uuid5(NAMESPACE_URL, "ump:registry:interface-check"))

# statuses the execution path knows (non-NULL exec_state)
CHECKED_STATUSES = frozenset(s for s in ALL_STATUSES if exec_state_for(s))
_PAGE = 200
_CONCURRENCY = 8


class InterfaceDriftService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        catalog: RegistryCatalogService,
        authorization: AuthorizationService,
        fetch: SafeFetchPort,
        remote_auth: RemoteAuthPort,
        cipher: CredentialCipherPort | None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._authz = authorization
        self._remote = RegistryRemote(fetch, remote_auth, cipher)
        self._clock = clock

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    async def check_all(self) -> list[InterfaceCheckResult]:
        """Check every model of every active server (the periodic run)."""
        models = await self._models()
        servers = await self._registry.get_provider_servers_by_ids(
            {m.provider_server_id for m in models}
        )
        pairs = [
            (servers[m.provider_server_id], m)
            for m in models
            if m.provider_server_id in servers
            and servers[m.provider_server_id].is_active
        ]
        return await self._check(pairs, actor=None)

    async def rediscover(self, auth: AuthContext, server_id: UUID) -> RediscoveryResult:
        """Check the models of one server now (owner or platform admin)."""
        self._authz.require_platform_role(auth, roles.PROVIDER, roles.PLATFORM_ADMIN)
        server = await self._registry.get_provider_server_by_id(server_id)
        if server is None or not owns_server(auth, server):
            raise problem(404, "Not Found", f"Server '{server_id}' not found.")
        models = await self._models(server.id)
        checked = await self._check(
            [(server, m) for m in models],
            actor=auth,
            audit=[
                AuditEvent.by(
                    auth,
                    "server.rediscover",
                    details={"server_id": str(server.id), "models": len(models)},
                )
            ],
        )
        return RediscoveryResult(server_id=server.id, checked=checked)

    async def _models(self, server_id: UUID | None = None) -> list[RegistryModel]:
        models: list[RegistryModel] = []
        after = None
        while True:
            page = await self._registry.list_models(
                ModelQuery(
                    visible_statuses=CHECKED_STATUSES,
                    provider_server_id=server_id,
                    after_slug=after,
                    limit=_PAGE,
                )
            )
            models.extend(page)
            if len(page) < _PAGE:
                return models
            after = page[-1].slug

    async def _check(
        self,
        pairs: list[tuple[ProviderServer, RegistryModel]],
        actor: AuthContext | None,
        audit: list[AuditEvent] = (),
    ) -> list[InterfaceCheckResult]:
        limit = asyncio.Semaphore(_CONCURRENCY)

        async def one(server: ProviderServer, model: RegistryModel):
            async with limit:
                return await self._check_one(server, model, actor)

        outcomes = await asyncio.gather(*(one(s, m) for s, m in pairs))
        applied = await self._registry.record_interface_checks(
            [check for check, _ in outcomes], audit=list(audit)
        )
        # a check skipped because the model was re-verified meanwhile reports
        # the state it found, but was not stored
        return [result for check, result in outcomes if check.model_id in applied]

    async def _check_one(
        self,
        server: ProviderServer,
        model: RegistryModel,
        actor: AuthContext | None,
    ) -> tuple[InterfaceCheck, InterfaceCheckResult]:
        now = self._clock()
        description, error = await self._describe(server, model)
        verified = model.verified_interface_sha256
        if description is None:
            state, sha = "unreachable", model.current_interface_sha256
        else:
            sha = interface_sha256(description)
            state = "drifted" if verified is not None and sha != verified else "in_sync"

        def event(action: str, **details) -> AuditEvent:
            if actor is None:  # the periodic check: no person acted
                return AuditEvent(
                    actor_sub=None, action=action, model_id=model.id, details=details
                )
            return AuditEvent.by(actor, action, model_id=model.id, details=details)

        previous = model.interface_state
        hash_changed = sha != model.current_interface_sha256
        audit = None
        if state == "drifted" and (previous != "drifted" or hash_changed):
            audit = event(
                "model.interface.drifted",
                sha256=sha,
                verified_sha256=verified,
                diff=interface_diff(model.verified_interface or {}, description),
            )
        elif state == "unreachable" and previous != "unreachable":
            audit = event("model.interface.unreachable", error=error)
        elif state == "in_sync" and previous in ("drifted", "unreachable"):
            audit = event("model.interface.restored", previous=previous, sha256=sha)
        elif (
            state == "in_sync"
            and verified is None
            and hash_changed
            and model.current_interface_sha256 is not None
        ):
            audit = event(
                "model.interface.changed",
                sha256=sha,
                previous_sha256=model.current_interface_sha256,
            )
        if audit is not None:
            logger.info(
                "[drift] %s model=%s %s -> %s",
                audit.action,
                model.slug,
                previous,
                state,
            )
        check = InterfaceCheck(
            model_id=model.id,
            expected_verified_sha256=verified,
            interface_state=state,
            current_interface_sha256=sha,
            last_checked_at=now,
            audit=audit,
        )
        return check, InterfaceCheckResult(
            model_id=model.id,
            slug=model.slug,
            remote_process_id=model.remote_process_id,
            previous_state=previous,
            interface_state=state,
            error=error,
        )

    async def _describe(
        self, server: ProviderServer, model: RegistryModel
    ) -> tuple[dict | None, str | None]:
        """The raw description, or (None, why) if it cannot be had."""
        try:
            description = await self._remote.get(
                server, f"processes/{model.remote_process_id}", lang=model.default_lang
            )
        except OGCProcessException as exc:
            return None, exc.response.detail
        if not isinstance(description, dict):
            return None, "Process description is not a JSON object."
        return description, None

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def interface(self, auth: AuthContext, model_id: UUID) -> InterfaceView:
        """Stored drift state plus a live comparison (owner, verifier, admin).

        Read-only: the live fetch is not stored; that is what checks are for.
        """
        if not auth.is_authenticated:
            raise problem(401, "Unauthorized", "Authentication required.")
        await self._catalog.get_model(auth, model_id)  # 404 unless visible
        model = await self._registry.get_model_by_id(model_id)
        if model is None:
            raise problem(404, "Not Found", f"Model '{model_id}' not found.")
        if not (
            model.owner_sub == auth.user_id
            or {roles.VERIFIER, roles.PLATFORM_ADMIN} & set(auth.roles)
        ):
            raise problem(
                403,
                "Forbidden",
                "The interface comparison is for the owner and verifiers.",
            )
        server = await self._registry.get_provider_server_by_id(
            model.provider_server_id
        )
        description, error = await self._describe(server, model)
        view = InterfaceView(
            model_id=model.id,
            interface_state=model.interface_state,
            last_checked_at=model.last_checked_at,
            verified_at=model.verified_at,
            verified_interface_sha256=model.verified_interface_sha256,
            verified_interface=model.verified_interface,
            current_interface_sha256=model.current_interface_sha256,
            live_error=error,
        )
        if description is not None:
            sha = interface_sha256(description)
            view.live = LiveInterface(sha256=sha, description=description)
            if model.verified_interface is not None:
                view.matches_verified = sha == model.verified_interface_sha256
                view.diff = interface_diff(model.verified_interface, description)
        return view


class InterfaceCheckScheduler:
    """Runs ``check_all`` every *interval* seconds while the app serves.

    Exactly one worker runs the checks: the one holding the poll lock under
    ``INTERFACE_CHECK_LOCK_KEY``. It keeps the lock until shutdown (with
    ``PgAdvisoryPollLock`` also until its connection dies), the others retry
    each interval, so a crashed leader is replaced after one interval.
    """

    def __init__(
        self,
        drift: InterfaceDriftService,
        lock: PollLockPort,
        interval: float,
        first_delay: float = 60.0,
    ) -> None:
        self._drift = drift
        self._lock = lock
        self._interval = interval
        self._first_delay = min(first_delay, interval)
        self._task: asyncio.Task | None = None
        self._leader = False

    async def __aenter__(self) -> "InterfaceCheckScheduler":
        self._task = asyncio.create_task(self._run(), name="ump-interface-check")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._leader:
            await self._lock.release(INTERFACE_CHECK_LOCK_KEY)
            self._leader = False

    async def run_once(self) -> bool:
        """One round: check if this worker is (or becomes) the leader."""
        if not self._leader:
            self._leader = await self._lock.try_acquire(INTERFACE_CHECK_LOCK_KEY)
        if not self._leader:
            return False
        try:
            results = await self._drift.check_all()
            logger.info("[drift] checked %d model(s)", len(results))
        except Exception as exc:  # keep the loop alive; next round retries
            logger.warning("[drift] interface check failed: %s", exc)
        return True

    async def _run(self) -> None:
        await asyncio.sleep(self._first_delay)
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval)
