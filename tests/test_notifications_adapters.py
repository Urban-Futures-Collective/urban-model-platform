"""Mail adapters: Jinja templates for every kind and language, SMTP error
mapping (with an in-test SMTP class), and the account side of notifications
(/me, PATCH /me/notifications, the role/language cache)."""

import asyncio
import smtplib

import pytest

from ump.adapters.mail import smtp_mailer
from ump.adapters.mail.jinja_renderer import JinjaNotificationRenderer
from ump.adapters.mail.smtp_mailer import SmtpMailer
from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository
from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.mailer import (
    EmailMessage,
    PermanentMailError,
    TransientMailError,
)
from ump.core.models.registry import AppUser
from ump.core.models.registry.notifications import KINDS
from ump.core.services.registry_accounts import RegistryAccountService, mail_language

MODEL = {"id": "m-1", "slug": "srv-echo", "process_id": "srv:echo", "name": "Echo"}
BASE = {
    "recipient_name": "Ada",
    "recipient_is_owner": False,
    "frontend_base_url": "https://ump-x.example",
}
PARAMS = {
    "model_submitted": [{}],
    "model_status": [
        {"from": f, "to": t, "note": "n"}
        for f, t in (
            ("submitted", "in_review"),
            ("published", "in_review"),
            ("in_review", "changes_requested"),
            ("in_review", "rejected"),
            ("in_review", "verified"),
            ("verified", "published"),
            ("published", "deactivated"),
        )
    ],
    "model_reverified": [{"note": None}],
    "model_interface": [
        {"state": "drifted", "error": None},
        {"state": "unreachable", "error": "timeout"},
        {"state": "restored"},
    ],
    "model_owner_transfer": [
        {
            "previous_owner": "a",
            "new_owner": "b",
            "previous_owner_name": "A",
            "new_owner_name": "B",
        }
    ],
    "access_requested": [
        {
            "access_request_id": "r-1",
            "user_sub": "u",
            "user_name": "U",
            "role": "srv:echo",
            "note": "thesis",
        }
    ],
    "access_decided": [
        {"decision": d, "access_request_id": "r-1", "role": "srv:echo", "note": "x"}
        for d in ("granted", "denied", "revoked")
    ],
    "job_complete": [
        {"job_id": "j-1", "process_id": "srv:echo", "status": s}
        for s in ("successful", "failed")
    ],
}


def test_sample_params_cover_every_kind():
    assert set(PARAMS) == KINDS


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize(
    "kind,params", [(k, p) for k, ps in sorted(PARAMS.items()) for p in ps]
)
def test_every_kind_renders_in_every_language(lang, kind, params):
    rendered = JinjaNotificationRenderer(["en", "de"]).render(
        kind, lang, {**BASE, "model": MODEL, **params}
    )
    assert rendered.subject and "\n" not in rendered.subject
    assert ("Hallo Ada," if lang == "de" else "Hello Ada,") in rendered.text
    assert "  " not in rendered.subject


def test_links_only_with_a_frontend_url():
    renderer = JinjaNotificationRenderer(["en", "de"])
    params = {**BASE, "model": MODEL, "note": None, "frontend_base_url": None}
    assert "http" not in renderer.render("model_reverified", "en", params).text
    with_link = renderer.render("model_submitted", "en", {**BASE, "model": MODEL})
    assert "https://ump-x.example/verify/m-1" in with_link.text


# UMP-X routes (ump-x-frontend, app/pages): /commons/{process id} is the public model
# page, /contribute/{model id} the owner's, /verify/{model id} the verifier's,
# /access the access manager, /jobs/{job id} a run.
LINKS = [
    # (kind, params, link for the owner, link for anyone else)
    ("model_submitted", {}, None, "/verify/m-1"),
    ("model_status", PARAMS["model_status"][0], "/contribute/m-1", None),
    ("model_reverified", PARAMS["model_reverified"][0], "/contribute/m-1", None),
    ("model_interface", PARAMS["model_interface"][0], "/contribute/m-1", "/verify/m-1"),
    (
        "model_owner_transfer",
        PARAMS["model_owner_transfer"][0],
        "/contribute/m-1",
        "/commons/srv:echo",
    ),
    ("access_requested", PARAMS["access_requested"][0], "/commons/srv:echo", "/access"),
    ("access_decided", PARAMS["access_decided"][0], None, "/commons/srv:echo"),
    ("job_complete", PARAMS["job_complete"][0], None, "/jobs/j-1"),
]


def test_link_cases_cover_every_kind():
    assert {kind for kind, *_ in LINKS} == KINDS


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("kind,params,owner_link,other_link", LINKS)
def test_links_point_at_the_frontend_routes(lang, kind, params, owner_link, other_link):
    renderer = JinjaNotificationRenderer(["en", "de"])
    for is_owner, link in ((True, owner_link), (False, other_link)):
        if link is None:  # this recipient never gets this mail
            continue
        text = renderer.render(
            kind,
            lang,
            {**BASE, "model": MODEL, **params, "recipient_is_owner": is_owner},
        ).text
        links = [w for w in text.split() if w.startswith("https://")]
        assert links == ["https://ump-x.example" + link]


def test_unknown_language_falls_back_to_the_default():
    rendered = JinjaNotificationRenderer(["de", "en"]).render(
        "model_submitted", "fr", {**BASE, "model": MODEL}
    )
    assert rendered.subject.startswith("Prüfung angefragt")


# --- SMTP --------------------------------------------------------------------


class FakeSMTP:
    """Stands in for smtplib.SMTP; behaviour set per test via ``fail``."""

    fail: Exception | None = None
    sent: list = []
    calls: list = []

    def __init__(self, host, port, timeout=None, **kwargs):
        FakeSMTP.calls.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSMTP.calls.append(("starttls",))

    def login(self, user, password):
        FakeSMTP.calls.append(("login", user))

    def send_message(self, message):
        if FakeSMTP.fail is not None:
            raise FakeSMTP.fail
        FakeSMTP.sent.append(message)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.fail, FakeSMTP.sent, FakeSMTP.calls = None, [], []
    monkeypatch.setattr(smtp_mailer.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def _send(mailer):
    asyncio.run(mailer.send(EmailMessage("to@x.org", "Sübject", "Bödy")))


def test_smtp_sends_with_starttls_and_login(smtp):
    _send(SmtpMailer("mail", 587, "UMP <noreply@x.org>", user="u", password="p"))
    assert smtp.calls == [("connect", "mail", 587), ("starttls",), ("login", "u")]
    (message,) = smtp.sent
    assert message["To"] == "to@x.org" and message["Subject"] == "Sübject"
    assert message["From"] == "UMP <noreply@x.org>" and message["Message-ID"]
    assert message.get_content().strip() == "Bödy"


def test_smtp_without_tls_or_login(smtp):
    _send(SmtpMailer("localhost", 1025, "noreply@x.org", security="none"))
    assert smtp.calls == [("connect", "localhost", 1025)]


@pytest.mark.parametrize(
    "error,kind",
    [
        (smtplib.SMTPRecipientsRefused({"to@x.org": (550, b"no")}), PermanentMailError),
        (smtplib.SMTPDataError(554, b"rejected"), PermanentMailError),
        (smtplib.SMTPDataError(451, b"try later"), TransientMailError),
        (smtplib.SMTPServerDisconnected("gone"), TransientMailError),
        (ConnectionRefusedError("refused"), TransientMailError),
        (TimeoutError("slow"), TransientMailError),
    ],
)
def test_smtp_error_mapping(smtp, error, kind):
    smtp.fail = error
    with pytest.raises(kind):
        _send(SmtpMailer("mail", 587, "noreply@x.org", security="none"))


# --- accounts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "locale,expected",
    [
        ("de", "de"),
        ("de-CH", "de"),
        ("de_DE", "de"),
        ("EN", "en"),
        ("fr", None),
        (None, None),
    ],
)
def test_mail_language(locale, expected):
    assert mail_language(locale, ["en", "de"]) == expected


def _accounts(repo):
    return RegistryAccountService(repo, languages=["en", "de"], touch_interval=0)


def test_touch_caches_platform_roles_and_language_only():
    repo = InMemoryRegistryRepository()
    auth = AuthContext(
        "u", ["verifier", "user", "srv:echo"], True, email="u@x.org", locale="de-AT"
    )
    asyncio.run(_accounts(repo).touch(auth))
    user = repo.users["u"]
    assert user.platform_roles == ["user", "verifier"]  # no model-access roles
    assert user.lang == "de" and user.notify == {}

    # an owner-FK upsert without claims never clears the cache
    asyncio.run(repo.upsert_user(AppUser(sub="u")))
    assert repo.users["u"].platform_roles == ["user", "verifier"]

    # losing a role shows on the next login
    asyncio.run(_accounts(repo).touch(AuthContext("u", ["user"], True)))
    assert repo.users["u"].platform_roles == ["user"] and repo.users["u"].lang == "de"


def test_me_and_notification_settings():
    repo = InMemoryRegistryRepository()
    accounts = _accounts(repo)
    auth = AuthContext("u", ["user"], True)
    me = asyncio.run(accounts.me(auth))
    assert me.lang == "en"
    assert me.notify == {
        "review": True,
        "access": True,
        "drift": True,
        "ownership": True,
        "jobs": False,
    }
    me = asyncio.run(accounts.set_notifications(auth, {"jobs": True}))
    me = asyncio.run(accounts.set_notifications(auth, {"drift": False}))
    assert me.notify["jobs"] is True and me.notify["drift"] is False
    assert repo.users["u"].notify == {"jobs": True, "drift": False}

    with pytest.raises(OGCProcessException) as exc:
        asyncio.run(accounts.set_notifications(auth, {"marketing": True}))
    assert exc.value.response.status == 400
    with pytest.raises(OGCProcessException):
        asyncio.run(accounts.set_notifications(AuthContext(None, [], False), {}))
