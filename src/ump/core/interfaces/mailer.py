"""MailerPort — sends one email (REF-F12 "Notifications").

Adapters translate their transport errors into the two kinds the dispatcher
acts on: a transient error (server down, timeout, 4xx) stops the run and the
same event is retried later; a permanent error (5xx, address rejected) skips
that recipient.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str


class TransientMailError(Exception):
    """Delivery may succeed later; retry the event."""


class PermanentMailError(Exception):
    """Delivery to this recipient will not succeed; skip them."""


class MailerPort(ABC):
    @abstractmethod
    async def send(self, message: EmailMessage) -> None:
        pass
