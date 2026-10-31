"""RegistryAccountService — the caller's account (REF-F12 "Identity").

``app_user`` caches the token claims of everyone who used the registry: it is
upserted on ``GET /me`` and, just-in-time, on any authenticated registry
request (``touch``), at most once per ``touch_interval`` per worker so reads do
not turn into writes.

Authorization always reads roles from the token. ``app_user.platform_roles``
only caches the platform roles of the last token so that mails can be
addressed to "all verifiers" (REF-F12 "Notifications"); ``lang`` is the mail
language from the token's ``locale``. ``notify`` holds the user's own
notification choices (``PATCH /me/notifications``).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Sequence

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.model_registry import ModelRegistryPort
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.registry import AppUser, roles
from ump.core.models.registry.notifications import (
    CATEGORIES,
    effective_preferences,
)
from ump.core.models.registry.views import MeView

logger = logging.getLogger(__name__)


class RegistryAccountService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        languages: Sequence[str] = ("en",),
        touch_interval: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._languages = list(languages)
        self._touch_interval = touch_interval
        self._monotonic = monotonic
        self._last_touch: dict[str, float] = {}

    async def me(self, auth: AuthContext) -> MeView:
        _require_user(auth)
        return self._view(auth, await self._upsert(auth))

    async def set_notifications(
        self, auth: AuthContext, choices: dict[str, bool]
    ) -> MeView:
        """Merge *choices* into the caller's notification preferences."""
        _require_user(auth)
        unknown = sorted(set(choices) - CATEGORIES)
        if unknown:
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Bad Request",
                    status=400,
                    detail=f"Unknown categories: {', '.join(unknown)}; "
                    f"known: {', '.join(sorted(CATEGORIES))}.",
                )
            )
        user = await self._upsert(auth)
        user = await self._registry.set_notify(user.sub, {**user.notify, **choices})
        return self._view(auth, user)

    def _view(self, auth: AuthContext, user: AppUser) -> MeView:
        return MeView(
            sub=user.sub,
            display_name=user.display_name,
            email=user.email,
            platform_roles=sorted(roles.PLATFORM_ROLES & set(auth.roles)),
            roles=sorted(set(auth.roles)),
            first_seen_at=user.first_seen_at,
            last_seen_at=user.last_seen_at,
            lang=user.lang or self._languages[0],
            notify=effective_preferences(user.notify),
        )

    async def touch(self, auth: AuthContext) -> None:
        """Record the caller in app_user, throttled. Never fails the request."""
        if not auth.is_authenticated or not auth.user_id:
            return
        last = self._last_touch.get(auth.user_id)
        if last is not None and self._monotonic() - last < self._touch_interval:
            return
        try:
            await self._upsert(auth)
        except Exception as exc:
            logger.warning("[registry] could not record user %s: %s", auth.user_id, exc)

    async def _upsert(self, auth: AuthContext) -> AppUser:
        user = await self._registry.upsert_user(
            AppUser(
                sub=auth.user_id,
                display_name=auth.name,
                email=auth.email,
                platform_roles=sorted(roles.PLATFORM_ROLES & set(auth.roles)),
                lang=mail_language(auth.locale, self._languages),
            )
        )
        self._last_touch[auth.user_id] = self._monotonic()
        return user


def mail_language(locale: str | None, languages: Sequence[str]) -> str | None:
    """The platform language matching a token ``locale`` (``de-CH`` → ``de``)."""
    if not locale:
        return None
    tag = locale.replace("_", "-")
    for candidate in (tag, tag.split("-", 1)[0].lower()):
        if candidate in languages:
            return candidate
    return None


def _require_user(auth: AuthContext) -> None:
    if not auth.is_authenticated or not auth.user_id:
        raise OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Unauthorized",
                status=401,
                detail="Authentication required.",
            )
        )
