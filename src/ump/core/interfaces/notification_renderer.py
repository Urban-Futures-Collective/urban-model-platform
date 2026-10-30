"""NotificationRendererPort — turns a planned mail into text (presentation).

``kind`` is one of ``notifications.KINDS``; ``params`` are the plan's
parameters plus what the dispatcher resolved for the recipient (model name in
their language, user names, the frontend base URL). Unknown languages fall
back to the platform default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RenderedMail:
    subject: str
    text: str


class NotificationRendererPort(ABC):
    @abstractmethod
    def render(self, kind: str, lang: str, params: dict[str, Any]) -> RenderedMail:
        pass
