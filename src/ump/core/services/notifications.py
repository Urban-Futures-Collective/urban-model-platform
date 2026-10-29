"""Email notifications (REF-F12 "Notifications", D14).

``NotificationDispatcher`` reads the audit log after its cursor — the log is
the outbox, every change wrote its row in the change's own transaction — plans
the mails of each event (``notifications.plan``), resolves recipients *at send
time*, renders and sends them, and advances the cursor event by event.

Delivery is at-least-once and in order:

- every recipient sent or skipped (no email, opted out, address rejected)
  → the cursor moves past the event;
- transient mail error → the run stops on that event; it is retried with
  exponential backoff, and after ``max_attempts`` skipped (logged) so one bad
  event cannot block the queue. Recipients reached before the error may get
  the mail twice.

``NotificationScheduler`` runs the dispatcher in exactly one worker (poll-lock
leader election, like ``InterfaceCheckScheduler``).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from ump.core.interfaces.audit_log import AuditLogPort, NotificationCursor
from ump.core.interfaces.mailer import (
    EmailMessage,
    MailerPort,
    PermanentMailError,
    TransientMailError,
)
from ump.core.interfaces.model_registry import ModelRegistryPort
from ump.core.interfaces.notification_renderer import NotificationRendererPort
from ump.core.interfaces.poll_lock import PollLockPort
from ump.core.models.providers_config import canonical_process_id
from ump.core.models.registry import AppUser
from ump.core.models.registry.audit import AuditRecord
from ump.core.models.registry.localized import resolve
from ump.core.models.registry.notifications import (
    ModelRef,
    PlannedMail,
    effective_preferences,
    plan,
)

logger = logging.getLogger(__name__)

# PollLockPort keys are UUID strings (see PgAdvisoryPollLock).
NOTIFICATION_LOCK_KEY = str(uuid5(NAMESPACE_URL, "ump:registry:notifications"))
MAX_BACKOFF_SECONDS = 900.0


@dataclass(frozen=True)
class DispatchRun:
    events: int = 0  # events the cursor moved past
    mails: int = 0  # mails handed to the mailer
    retry: bool = False  # stopped on a transient error; back off


@dataclass(frozen=True)
class _Recipient:
    email: str
    lang: str
    name: str | None = None


class NotificationDispatcher:
    def __init__(
        self,
        audit_log: AuditLogPort,
        registry: ModelRegistryPort,
        renderer: NotificationRendererPort,
        mailer: MailerPort,
        *,
        languages: Sequence[str],
        fallback_admin_emails: Sequence[str] = (),
        frontend_base_url: str | None = None,
        settle_seconds: float = 5.0,
        max_attempts: int = 10,
        batch_size: int = 100,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._log = audit_log
        self._registry = registry
        self._renderer = renderer
        self._mailer = mailer
        self._languages = list(languages)
        self._fallback = list(fallback_admin_emails)
        self._frontend = frontend_base_url.rstrip("/") if frontend_base_url else None
        self._settle = timedelta(seconds=settle_seconds)
        self._max_attempts = max_attempts
        self._batch = batch_size
        self._clock = clock

    async def run_once(self) -> DispatchRun:
        cursor = await self._log.get_notification_cursor()
        records = await self._log.list_after(
            cursor.last_audit_id, self._clock() - self._settle, self._batch
        )
        events = mails = 0
        for record in records:
            try:
                mails += await self.deliver(record)
            except TransientMailError as exc:
                attempts = (
                    cursor.attempts + 1 if cursor.failing_audit_id == record.id else 1
                )
                if attempts < self._max_attempts:
                    logger.warning(
                        "[notify] audit event %d: attempt %d failed: %s",
                        record.id,
                        attempts,
                        exc,
                    )
                    await self._log.save_notification_cursor(
                        replace(cursor, failing_audit_id=record.id, attempts=attempts)
                    )
                    return DispatchRun(events, mails, retry=True)
                logger.error(
                    "[notify] audit event %d (%s) skipped after %d attempts: %s",
                    record.id,
                    record.action,
                    attempts,
                    exc,
                )
            cursor = NotificationCursor(last_audit_id=record.id)
            await self._log.save_notification_cursor(cursor)
            events += 1
        return DispatchRun(events, mails)

    def backoff(self, attempts: int, interval: float) -> float:
        """Seconds to wait before retrying an event that failed *attempts* times."""
        return min(interval * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS)

    async def attempts(self) -> int:
        return (await self._log.get_notification_cursor()).attempts

    # ------------------------------------------------------------------
    # One event
    # ------------------------------------------------------------------

    async def deliver(self, record: AuditRecord) -> int:
        """Send the mails of *record*; the number sent. Transient errors raise."""
        model = await self._model(record.model_id) if record.model_id else None
        planned = plan(record, model, self._fallback)
        if not planned:
            return 0
        names = await self._names(record, model)
        sent = 0
        seen: set[str] = set()  # one mail per address and event
        for mail in planned:
            for recipient in await self._recipients(record, mail):
                if recipient.email.lower() in seen:
                    continue
                seen.add(recipient.email.lower())
                params = self._params(mail, recipient, model, names)
                try:
                    rendered = self._renderer.render(mail.kind, recipient.lang, params)
                except Exception as exc:  # a template bug must not block the queue
                    logger.error("[notify] cannot render %s: %s", mail.kind, exc)
                    continue
                try:
                    await self._mailer.send(
                        EmailMessage(
                            to=recipient.email,
                            subject=rendered.subject,
                            text=rendered.text,
                        )
                    )
                    sent += 1
                except PermanentMailError as exc:
                    logger.warning(
                        "[notify] %s to %s rejected: %s",
                        mail.kind,
                        recipient.email,
                        exc,
                    )
        return sent

    async def _model(self, model_id: UUID) -> ModelRef | None:
        model = await self._registry.get_model_by_id(model_id)
        if model is None:
            return None
        server = await self._registry.get_provider_server_by_id(
            model.provider_server_id
        )
        return ModelRef(
            id=model.id,
            slug=model.slug,
            process_id=canonical_process_id(server.name, model.remote_process_id),
            owner_sub=model.owner_sub,
            full_name=model.full_name,
            default_lang=model.default_lang,
        )

    async def _names(
        self, record: AuditRecord, model: ModelRef | None
    ) -> dict[str, AppUser]:
        """Accounts a mail may mention by name (actor, user, owners)."""
        details = record.details or {}
        subs = {
            s
            for s in (
                record.actor_sub,
                details.get("user_sub"),
                details.get("before"),
                details.get("after"),
                model.owner_sub if model else None,
            )
            if isinstance(s, str) and s
        }
        return await self._registry.get_users(subs) if subs else {}

    async def _recipients(
        self, record: AuditRecord, mail: PlannedMail
    ) -> list[_Recipient]:
        users: list[AppUser] = []
        addresses: list[str] = []
        subs = {t.sub for t in mail.targets if t.sub}
        if subs:
            found = await self._registry.get_users(subs)
            users.extend(found[s] for s in sorted(subs) if s in found)
        for target in mail.targets:
            if target.role:
                users.extend(await self._registry.list_users_with_role(target.role))
            if target.email:
                addresses.append(target.email)

        recipients: list[_Recipient] = []
        for user in users:
            if user.sub == record.actor_sub and not mail.to_actor:
                continue
            if not user.email:
                continue
            if not effective_preferences(user.notify).get(mail.category, False):
                continue
            recipients.append(
                _Recipient(
                    email=user.email,
                    lang=user.lang if user.lang in self._languages else self._lang(),
                    name=user.display_name,
                )
            )
        recipients.extend(_Recipient(email=a, lang=self._lang()) for a in addresses)
        return recipients

    def _lang(self) -> str:
        return self._languages[0] if self._languages else "en"

    def _params(
        self,
        mail: PlannedMail,
        recipient: _Recipient,
        model: ModelRef | None,
        names: dict[str, AppUser],
    ) -> dict[str, Any]:
        def name(sub: Any) -> Any:
            user = names.get(sub) if isinstance(sub, str) else None
            return (user.display_name if user else None) or sub

        params: dict[str, Any] = {
            **mail.params,
            "recipient_name": recipient.name,
            "frontend_base_url": self._frontend,
        }
        if model is not None:
            params["model"] = {
                "id": str(model.id),
                "slug": model.slug,
                "process_id": model.process_id,
                "name": resolve(model.full_name, recipient.lang, model.default_lang),
            }
        for key in ("user_sub", "previous_owner", "new_owner"):
            if key in params:
                params[key.replace("_sub", "") + "_name"] = name(params[key])
        return params


class NotificationScheduler:
    """Runs the dispatcher every *interval* seconds in one worker.

    The worker holding the poll lock under ``NOTIFICATION_LOCK_KEY`` keeps it
    until shutdown; the others retry each interval. After a transient mail
    error the leader backs off exponentially (capped at 15 minutes).
    """

    def __init__(
        self,
        dispatcher: NotificationDispatcher,
        lock: PollLockPort,
        interval: float,
    ) -> None:
        self._dispatcher = dispatcher
        self._lock = lock
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._leader = False

    async def __aenter__(self) -> "NotificationScheduler":
        self._task = asyncio.create_task(self._run(), name="ump-notifications")
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._leader:
            await self._lock.release(NOTIFICATION_LOCK_KEY)
            self._leader = False

    async def run_once(self) -> float:
        """One round; returns the seconds to wait before the next one."""
        if not self._leader:
            self._leader = await self._lock.try_acquire(NOTIFICATION_LOCK_KEY)
        if not self._leader:
            return self._interval
        try:
            result = await self._dispatcher.run_once()
            if result.mails:
                logger.info(
                    "[notify] %d mail(s) for %d event(s)", result.mails, result.events
                )
            if result.retry:
                attempts = await self._dispatcher.attempts()
                return self._dispatcher.backoff(attempts, self._interval)
        except Exception as exc:  # keep the loop alive; next round retries
            logger.warning("[notify] dispatch failed: %s", exc)
        return self._interval

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(await self.run_once())
