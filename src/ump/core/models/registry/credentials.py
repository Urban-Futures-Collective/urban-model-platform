"""Stored model-server credentials and whether UMP can use them (REF-F12).

``read_credentials`` is the single rule for turning a server's stored
ciphertext into an outbound ``AuthConfig``. The database providers adapter
uses it to build the OGC catalogue, and the registry API uses it to report
``credentials_state``, so a server the catalogue leaves out is reported with
the same reason the catalogue had.

States:

- ``not_required``: ``NoAuth``, nothing to decrypt;
- ``ok``: decrypted and valid for the auth type;
- ``missing``: the auth type needs credentials but none are stored;
- ``missing_key``: no ``UMP_REGISTRY_CREDENTIALS_KEY`` configured;
- ``undecryptable``: the configured key(s) cannot decrypt them (key changed);
- ``invalid``: decrypted, but not valid for the auth type.

Every state other than ``not_required`` and ``ok`` keeps the server's models
out of ``/v1.0/processes``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from ump.core.interfaces.credential_cipher import (
    CredentialCipherPort,
    CredentialDecryptionError,
)
from ump.core.models.providers_config import (
    AuthConfig,
    NoAuthConfig,
    auth_config_from_secret,
)

CredentialsState = Literal[
    "not_required", "ok", "missing", "missing_key", "undecryptable", "invalid"
]

USABLE_STATES: frozenset[str] = frozenset({"not_required", "ok"})


@dataclass(frozen=True)
class StoredCredentials:
    state: CredentialsState
    auth: AuthConfig | None = None  # set for the usable states
    detail: str | None = None  # why they are not usable

    @property
    def usable(self) -> bool:
        return self.state in USABLE_STATES


def read_credentials(
    auth_type: str,
    credentials: bytes | None,
    cipher: CredentialCipherPort | None,
) -> StoredCredentials:
    """Decrypt *credentials* for *auth_type*; never raises."""
    if auth_type == "NoAuth":
        return StoredCredentials("not_required", auth=NoAuthConfig())
    if credentials is None:
        return StoredCredentials(
            "missing", detail=f"{auth_type} server has no credentials stored"
        )
    if cipher is None:
        return StoredCredentials(
            "missing_key",
            detail=f"{auth_type} credentials need UMP_REGISTRY_CREDENTIALS_KEY",
        )
    try:
        plaintext = cipher.decrypt(credentials)
    except CredentialDecryptionError as exc:
        return StoredCredentials("undecryptable", detail=str(exc))
    try:
        auth = auth_config_from_secret(auth_type, json.loads(plaintext))
    except (ValueError, TypeError) as exc:  # bad JSON or failed validation
        return StoredCredentials(
            "invalid", detail=f"stored {auth_type} credentials are invalid: {exc}"
        )
    return StoredCredentials("ok", auth=auth)
