"""SmtpMailer — MailerPort over SMTP (stdlib ``smtplib``, run in a thread).

One connection per mail: notifications are rare, and a fresh connection
never trips over a server-side idle timeout. Errors are mapped to the port's
two kinds: 5xx replies and refused addresses are permanent (skip the
recipient), everything else — connection errors, timeouts, 4xx — transient
(retry the event later).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid
from typing import Literal

from ump.core.interfaces.mailer import (
    EmailMessage,
    MailerPort,
    PermanentMailError,
    TransientMailError,
)

Security = Literal["starttls", "ssl", "none"]


class SmtpMailer(MailerPort):
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        security: Security = "starttls",
        user: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._security = security
        self._user = user
        self._password = password
        self._timeout = timeout

    async def send(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"] = self._sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=False)
        mime["Message-ID"] = make_msgid(domain=self._sender.rpartition("@")[2] or None)
        mime.set_content(message.text)
        try:
            await asyncio.to_thread(self._send, mime)
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentMailError(str(exc)) from exc
        except smtplib.SMTPResponseException as exc:
            kind = PermanentMailError if exc.smtp_code >= 500 else TransientMailError
            raise kind(f"{exc.smtp_code} {exc.smtp_error!r}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise TransientMailError(str(exc)) from exc

    def _send(self, mime: MimeMessage) -> None:
        context = ssl.create_default_context()
        if self._security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context
            )
        else:
            client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with client:
            if self._security == "starttls":
                client.starttls(context=context)
            if self._user:
                client.login(self._user, self._password or "")
            client.send_message(mime)
