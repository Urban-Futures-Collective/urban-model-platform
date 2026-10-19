"""RegistryAccountService — the caller's account (REF-F12 "Identity").

``app_user`` caches the token claims of everyone who used the registry: it is
upserted on ``GET /me`` and, just-in-time, on any authenticated registry
request (``touch``), at most once per ``touch_interval`` per worker so reads do
not turn into writes. Roles are never stored; they come from the token.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.model_registry import ModelRegistryPort
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.registry import AppUser, roles
from ump.core.models.registry.views import MeView

logger = logging.getLogger(__name__)


class RegistryAccountService:
    def __init__(
        self,
        registry: ModelRegistryPort,
        touch_interval: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._touch_interval = touch_interval
        self._monotonic = monotonic
        self._last_touch: dict[str, float] = {}

    async def me(self, auth: AuthContext) -> MeView:
        if not auth.is_authenticated or not auth.user_id:
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Unauthorized",
                    status=401,
                    detail="Authentication required.",
                )
            )
        user = await self._upsert(auth)
        return MeView(
            sub=user.sub,
            display_name=user.display_name,
            email=user.email,
            platform_roles=sorted(roles.PLATFORM_ROLES & set(auth.roles)),
            roles=sorted(set(auth.roles)),
            first_seen_at=user.first_seen_at,
            last_seen_at=user.last_seen_at,
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
            AppUser(sub=auth.user_id, display_name=auth.name, email=auth.email)
        )
        self._last_touch[auth.user_id] = self._monotonic()
        return user
