"""Talking to a registered model server: URL checks, credentials, descriptions.

Shared by the onboarding and verification services. Every request goes
through ``SafeFetchPort``; stored credentials are decrypted only to build the
outbound auth headers.
"""

from __future__ import annotations

import json

from ump.core.interfaces.credential_cipher import (
    CredentialCipherPort,
    CredentialDecryptionError,
)
from ump.core.interfaces.remote_auth import RemoteAuthPort
from ump.core.interfaces.safe_fetch import (
    RemoteFetchError,
    SafeFetchPort,
    UnsafeUrlError,
)
from ump.core.models.providers_config import (
    AuthConfig,
    NoAuthConfig,
    auth_config_from_secret,
)
from ump.core.models.registry import ProviderServer
from ump.core.services.registry_common import problem


def _with_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


class RegistryRemote:
    def __init__(
        self,
        fetch: SafeFetchPort,
        remote_auth: RemoteAuthPort,
        cipher: CredentialCipherPort | None,
    ) -> None:
        self._fetch = fetch
        self._remote_auth = remote_auth
        self._cipher = cipher

    def safe_url(self, url: str) -> str:
        url = _with_slash(url)
        try:
            self._fetch.check_url(url)
        except UnsafeUrlError as exc:
            raise problem(400, "Bad Request", f"base_url not allowed: {exc}")
        return url

    def encrypt(self, auth: AuthConfig | None) -> tuple[str, bytes | None]:
        if auth is None or isinstance(auth, NoAuthConfig):
            return "NoAuth", None
        if self._cipher is None:
            raise problem(
                503,
                "Service Unavailable",
                "Credential storage is not configured (UMP_REGISTRY_CREDENTIALS_KEY).",
            )
        secret = {
            k: v.get_secret_value() if hasattr(v, "get_secret_value") else v
            for k, v in auth
            if k != "type"
        }
        return auth.type, self._cipher.encrypt(json.dumps(secret))

    def outbound_auth(self, server: ProviderServer) -> AuthConfig:
        if server.auth_type == "NoAuth" or server.credentials is None:
            return NoAuthConfig()
        if self._cipher is None:
            raise problem(
                503,
                "Service Unavailable",
                "Credential storage is not configured (UMP_REGISTRY_CREDENTIALS_KEY).",
            )
        try:
            secret = json.loads(self._cipher.decrypt(server.credentials))
        except CredentialDecryptionError:
            raise problem(
                409,
                "Conflict",
                "Stored credentials cannot be decrypted; set them again.",
            )
        return auth_config_from_secret(server.auth_type, secret)

    async def get(self, server: ProviderServer, path: str, lang: str | None = None):
        headers = dict(self._remote_auth.resolve(self.outbound_auth(server)).headers)
        if lang:
            headers["Accept-Language"] = lang
        url = server.base_url + path
        try:
            return await self._fetch.get_json(url, headers=headers)
        except UnsafeUrlError as exc:
            raise problem(400, "Bad Request", f"{url} not allowed: {exc}")
        except RemoteFetchError as exc:
            raise problem(502, "Bad Gateway", f"{url}: {exc}")
