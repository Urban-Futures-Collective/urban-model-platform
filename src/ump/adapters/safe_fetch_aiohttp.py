"""aiohttp implementation of SafeFetchPort (REF-F12 "Outbound request safety").

- Scheme ``https``; ``http`` only with ``allow_http`` (dev).
- No credentials in the URL; a host is required.
- Addresses are checked **when connecting**: a resolver wrapper drops private,
  loopback, link-local, multicast, reserved and unspecified addresses (this
  covers cloud metadata endpoints such as 169.254.169.254), so a DNS answer
  cannot be swapped between check and use. IP literals, which aiohttp does not
  resolve, are checked in ``check_url``. Hosts in ``allowed_private_hosts``
  (in-cluster model servers) skip the address check.
- Redirects are not followed; a 3xx is an error.
- ``timeout`` seconds in total; at most ``max_bytes`` of body.

A session is opened per call: discovery is rare and must not share connection
state with the execution client.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any, Iterable
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver

from ump.core.interfaces.safe_fetch import (
    RemoteFetchError,
    SafeFetchPort,
    UnsafeUrlError,
)


class _BlockedAddress(OSError):
    """Raised inside the resolver; aiohttp wraps OSErrors from resolution."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


class _GuardedResolver(AbstractResolver):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self._inner = DefaultResolver()
        self._allowed = allowed_hosts

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        if host.lower() in self._allowed:
            return results
        public = [r for r in results if _is_public(r["host"])]
        if not public:
            raise _BlockedAddress(f"{host} resolves only to non-public addresses")
        return public

    async def close(self) -> None:
        await self._inner.close()


class AioHttpSafeFetch(SafeFetchPort):
    def __init__(
        self,
        allow_http: bool = False,
        allowed_private_hosts: Iterable[str] = (),
        timeout: float = 10.0,
        max_bytes: int = 2_000_000,
    ) -> None:
        self._allow_http = allow_http
        self._allowed = frozenset(h.lower() for h in allowed_private_hosts)
        self._timeout = timeout
        self._max_bytes = max_bytes

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        schemes = {"https", "http"} if self._allow_http else {"https"}
        if parts.scheme not in schemes:
            raise UnsafeUrlError(f"scheme must be {' or '.join(sorted(schemes))}")
        if parts.username or parts.password:
            raise UnsafeUrlError("credentials in the URL are not allowed")
        host = (parts.hostname or "").lower()
        if not host:
            raise UnsafeUrlError("no host")
        if host in self._allowed:
            return
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            return  # a name: checked when it is resolved
        if not _is_public(str(literal)):
            raise UnsafeUrlError(f"{host} is not a public address")

    async def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        self.check_url(url)
        connector = aiohttp.TCPConnector(resolver=_GuardedResolver(self._allowed))
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as session:
                async with session.get(
                    url,
                    headers={"Accept": "application/json", **(headers or {})},
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status < 400:
                        raise RemoteFetchError(
                            f"HTTP {response.status}: redirects are not followed"
                        )
                    if response.status >= 400:
                        raise RemoteFetchError(f"HTTP {response.status}")
                    body = await self._read_capped(response)
        except aiohttp.ClientConnectorError as exc:
            if isinstance(exc.__cause__, _BlockedAddress) or isinstance(
                getattr(exc, "os_error", None), _BlockedAddress
            ):
                raise UnsafeUrlError(str(exc.__cause__ or exc.os_error)) from exc
            raise RemoteFetchError(f"cannot connect: {exc}") from exc
        except TimeoutError as exc:
            raise RemoteFetchError(f"no answer within {self._timeout}s") from exc
        except aiohttp.ClientError as exc:
            raise RemoteFetchError(str(exc)) from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise RemoteFetchError("response is not JSON") from exc

    async def _read_capped(self, response: aiohttp.ClientResponse) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > self._max_bytes:
                raise RemoteFetchError(f"response larger than {self._max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
