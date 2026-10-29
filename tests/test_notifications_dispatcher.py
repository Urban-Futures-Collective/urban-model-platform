"""NotificationDispatcher / NotificationScheduler with in-memory adapters and
in-test mailer, renderer and lock (REF-F12 "Notifications")."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ump.adapters.audit_log_inmemory import InMemoryAuditLog
from ump.adapters.mail.jinja_renderer import JinjaNotificationRenderer
from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository
from ump.core.interfaces.audit_log import NotificationCursor
from ump.core.interfaces.mailer import (
    MailerPort,
    PermanentMailError,
    TransientMailError,
)
from ump.core.interfaces.notification_renderer import (
    NotificationRendererPort,
    RenderedMail,
)
from ump.core.interfaces.poll_lock import PollLockPort
from ump.core.models.registry import AppUser, ProviderServer, RegistryModel
from ump.core.models.registry.audit import AuditEvent
from ump.core.services.notifications import (
    NotificationDispatcher,
    NotificationScheduler,
)

SEED = AuditEvent(actor_sub=None, action="test.seed")


class Mailer(MailerPort):
    def __init__(self):
        self.sent = []
        self.transient_failures = 0  # fail this many sends, then work
        self.rejected: set[str] = set()

    async def send(self, message):
        if self.transient_failures:
            self.transient_failures -= 1
            raise TransientMailError("smtp down")
        if message.to in self.rejected:
            raise PermanentMailError("550 no such user")
        self.sent.append(message)


class Renderer(NotificationRendererPort):
    def __init__(self, broken_kinds=()):
        self.params = []
        self.broken = set(broken_kinds)

    def render(self, kind, lang, params):
        if kind in self.broken:
            raise KeyError("template bug")
        self.params.append(params)
        return RenderedMail(subject=f"{kind}|{lang}", text=str(params))


class Env:
    def __init__(self, renderer=None, **dispatcher_kwargs):
        self.log = InMemoryAuditLog()
        self.repo = InMemoryRegistryRepository()  # its seed rows stay out of the log
        self.mailer = Mailer()
        self.renderer = renderer or Renderer()
        self.model = asyncio.run(self._seed())
        kwargs = {"settle_seconds": 0, "clock": lambda: _now() + timedelta(seconds=1)}
        kwargs.update(dispatcher_kwargs)
        self.dispatcher = NotificationDispatcher(
            self.log,
            self.repo,
            self.renderer,
            self.mailer,
            languages=["en", "de"],
            **kwargs,
        )

    async def _seed(self):
        for sub, email, platform_roles, lang in (
            ("owner", "owner@x.org", ["provider"], "de"),
            ("ver-1", "v1@x.org", ["verifier"], None),
            ("ver-2", "v2@x.org", ["verifier"], "en"),
            ("ver-noemail", None, ["verifier"], None),
            ("adm-1", "a1@x.org", ["access-admin"], None),
            ("user", "user@x.org", ["user"], None),
        ):
            await self.repo.upsert_user(
                AppUser(
                    sub=sub,
                    email=email,
                    display_name=sub.upper(),
                    platform_roles=platform_roles,
                    lang=lang,
                )
            )
        server = await self.repo.create_provider_server(
            ProviderServer(name="srv", base_url="https://srv/"), audit=SEED
        )
        (model,) = await self.repo.create_models(
            [
                RegistryModel(
                    slug="srv-echo",
                    provider_server_id=server.id,
                    remote_process_id="echo",
                    owner_sub="owner",
                    full_name={"en": "Echo", "de": "Echo (de)"},
                )
            ],
            audit=[SEED],
        )
        return model

    def event(self, action, actor="actor", outcome="success", **details):
        asyncio.run(
            self.log.append(
                AuditEvent(
                    actor_sub=actor,
                    action=action,
                    model_id=None if action.startswith("job.") else self.model.id,
                    job_id="job-1" if action.startswith("job.") else None,
                    outcome=outcome,
                    details=details,
                )
            )
        )

    def run(self):
        return asyncio.run(self.dispatcher.run_once())

    def sent_to(self):
        return sorted(m.to for m in self.mailer.sent)


def _now():
    return datetime.now(timezone.utc)


def test_submission_mails_every_verifier_with_an_email_and_moves_the_cursor():
    env = Env()
    env.event(
        "model.status.change", actor="owner", **{"from": "draft", "to": "submitted"}
    )
    result = env.run()
    assert (result.events, result.mails, result.retry) == (1, 2, False)
    assert env.sent_to() == ["v1@x.org", "v2@x.org"]
    assert env.log.cursor == NotificationCursor(last_audit_id=1)
    assert env.run().events == 0  # nothing twice


def test_actor_is_not_told_about_their_own_action():
    env = Env()
    env.event("model.interface.drifted", actor="ver-1")  # a verifier's rediscover
    env.run()
    assert env.sent_to() == ["owner@x.org", "v2@x.org"]


def test_language_names_and_links_are_per_recipient():
    env = Env(frontend_base_url="https://ump-x.example/")
    env.event("access.request", actor="user", user_sub="user", role="srv:echo")
    env.run()
    assert env.sent_to() == ["a1@x.org", "owner@x.org"]
    by_lang = {
        m.subject.split("|")[1]: p for m, p in zip(env.mailer.sent, env.renderer.params)
    }
    assert by_lang["de"]["model"]["name"] == "Echo (de)"  # owner reads German
    assert by_lang["en"]["model"]["name"] == "Echo"
    assert by_lang["en"]["user_name"] == "USER"
    assert by_lang["en"]["frontend_base_url"] == "https://ump-x.example"
    # the owner gets the owner's link, the access admin the access page
    assert by_lang["de"]["recipient_is_owner"] is True
    assert by_lang["en"]["recipient_is_owner"] is False


def test_opt_out_is_respected():
    env = Env()
    asyncio.run(env.repo.set_notify("ver-1", {"drift": False}))
    env.event("model.interface.unreachable", actor=None, error="timeout")
    env.run()
    assert env.sent_to() == ["owner@x.org", "v2@x.org"]


def test_fallback_mailbox_is_added_and_addresses_are_deduplicated():
    env = Env(fallback_admin_emails=["box@x.org", "A1@x.org"])
    env.event("access.request", actor="user", user_sub="user", role="srv:echo")
    env.run()
    assert env.sent_to() == ["a1@x.org", "box@x.org", "owner@x.org"]


def test_job_mails_are_opt_in_and_go_to_the_job_owner():
    env = Env()
    env.event("job.complete", actor="user", outcome="failure", status="failed")
    env.run()
    assert env.sent_to() == []
    asyncio.run(env.repo.set_notify("user", {"jobs": True}))
    env.event("job.complete", actor="user", outcome="failure", status="failed")
    env.run()
    assert env.sent_to() == ["user@x.org"]


def test_transient_error_keeps_the_cursor_and_retries():
    env = Env()
    env.event("access.grant", actor="adm-1", user_sub="user", role="srv:echo")
    env.event("access.deny", actor="adm-1", user_sub="user", note="no")
    env.mailer.transient_failures = 1
    first = env.run()
    assert first.retry and first.events == 0
    assert env.log.cursor == NotificationCursor(0, failing_audit_id=1, attempts=1)
    second = env.run()
    assert (second.events, second.retry) == (2, False)
    assert env.log.cursor == NotificationCursor(last_audit_id=2)
    assert len(env.mailer.sent) == 2


def test_event_is_skipped_after_max_attempts():
    env = Env(max_attempts=2)
    env.event("access.grant", actor="adm-1", user_sub="user", role="srv:echo")
    env.event("access.revoke", actor="adm-1", user_sub="user", note="gone")
    env.mailer.transient_failures = 2
    assert env.run().retry
    result = env.run()  # second failure of event 1: skipped, event 2 sent
    assert result.events == 2 and not result.retry
    assert [m.subject for m in env.mailer.sent] == ["access_decided|en"]
    assert env.log.cursor == NotificationCursor(last_audit_id=2)


def test_permanent_error_skips_only_that_recipient():
    env = Env()
    env.mailer.rejected = {"v1@x.org"}
    env.event(
        "model.status.change", actor="owner", **{"from": "draft", "to": "submitted"}
    )
    result = env.run()
    assert result.events == 1 and env.sent_to() == ["v2@x.org"]


def test_render_error_skips_the_mail_not_the_queue():
    env = Env(renderer=Renderer(broken_kinds={"model_submitted"}))
    env.event(
        "model.status.change", actor="owner", **{"from": "draft", "to": "submitted"}
    )
    env.event("model.reverify", actor="ver-1")
    assert env.run().events == 2
    assert env.sent_to() == ["owner@x.org"]


def test_rows_younger_than_the_settle_time_wait():
    env = Env(settle_seconds=60, clock=_now)
    env.event("model.reverify", actor="ver-1")
    assert env.run().events == 0
    assert env.log.cursor.last_audit_id == 0


def test_real_templates_render_through_the_dispatcher():
    env = Env(renderer=JinjaNotificationRenderer(["en", "de"]))
    env.event(
        "model.status.change",
        actor="ver-1",
        **{"from": "in_review", "to": "changes_requested", "note": "Add a licence"},
    )
    env.run()
    (mail,) = env.mailer.sent
    assert mail.to == "owner@x.org"
    assert mail.subject == "Echo (de) benötigt Änderungen"
    assert "Add a licence" in mail.text and "Hallo OWNER," in mail.text


class OneLeader(PollLockPort):
    def __init__(self):
        self.holder = None

    async def try_acquire(self, job_id):
        return self.holder is None and not setattr(self, "holder", job_id)

    async def release(self, job_id):
        self.holder = None


def test_only_the_lock_holder_dispatches_and_backs_off_on_errors():
    env = Env()
    lock = OneLeader()
    leader = NotificationScheduler(env.dispatcher, lock, interval=30)
    other = NotificationScheduler(env.dispatcher, lock, interval=30)
    env.event("model.reverify", actor="ver-1")
    env.mailer.transient_failures = 1

    assert asyncio.run(leader.run_once()) == 30  # first failure: one interval
    assert asyncio.run(other.run_once()) == 30
    assert env.mailer.sent == []
    assert asyncio.run(leader.run_once()) == 30
    assert env.sent_to() == ["owner@x.org"]


@pytest.mark.parametrize("attempts,expected", [(1, 30), (2, 60), (3, 120), (10, 900)])
def test_backoff_is_exponential_and_capped(attempts, expected):
    assert Env().dispatcher.backoff(attempts, 30) == expected
