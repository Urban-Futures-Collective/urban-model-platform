"""SafeFetchPort — outbound requests to URLs chosen by users (REF-F12
"Outbound request safety").

Registering a server makes UMP fetch a user-supplied URL; without checks that
is a server-side request forgery path into the cluster. Adapters must enforce,
at connection time (so DNS rebinding cannot bypass it):

- scheme ``https`` (``http`` only if explicitly allowed, dev only);
- no private, loopback, link-local, multicast or metadata addresses unless the
  host is allowlisted (in-cluster model servers);
- no redirects; a timeout; a response size cap.

Used for discovery and (phase D) drift checks. Ordinary execution traffic to
already registered servers keeps using ``HttpClientPort``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class UnsafeUrlError(Exception):
    """The URL is not allowed as an outbound target."""


class RemoteFetchError(Exception):
    """The remote answered with an error, invalid JSON, too much data, or not at all."""


class SafeFetchPort(ABC):
    @abstractmethod
    def check_url(self, url: str) -> None:
        """Static checks (scheme, host syntax, literal IPs, allowlist).

        Raises ``UnsafeUrlError``. Address checks for host names happen at
        connection time in ``get_json``.
        """

    @abstractmethod
    async def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        """GET *url* and parse JSON.

        Raises ``UnsafeUrlError`` or ``RemoteFetchError``.
        """
