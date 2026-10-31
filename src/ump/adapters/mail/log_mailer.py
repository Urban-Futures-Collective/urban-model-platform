"""LogMailer — writes mails to the log instead of sending them (dev)."""

from __future__ import annotations

import logging

from ump.core.interfaces.mailer import EmailMessage, MailerPort

logger = logging.getLogger(__name__)


class LogMailer(MailerPort):
    async def send(self, message: EmailMessage) -> None:
        logger.info(
            "[mail] to=%s subject=%s\n%s", message.to, message.subject, message.text
        )
