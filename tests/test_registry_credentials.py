"""read_credentials: the one rule for stored model-server credentials (REF-F12).

Pure core; in-test CredentialCipherPort implementations instead of mocks.
"""

import json

import pytest

from ump.core.interfaces.credential_cipher import (
    CredentialCipherPort,
    CredentialDecryptionError,
)
from ump.core.models.providers_config import (
    ApiKeyAuthConfig,
    BasicAuthConfig,
    BearerTokenAuthConfig,
    NoAuthConfig,
)
from ump.core.models.registry.credentials import read_credentials


class PlainCipher(CredentialCipherPort):
    """Reversible stand-in: the 'ciphertext' is the UTF-8 plaintext."""

    def encrypt(self, plaintext: str) -> bytes:
        return plaintext.encode()

    def decrypt(self, ciphertext: bytes) -> str:
        return bytes(ciphertext).decode()


class WrongKeyCipher(CredentialCipherPort):
    def encrypt(self, plaintext: str) -> bytes:
        raise AssertionError("not used")

    def decrypt(self, ciphertext: bytes) -> str:
        raise CredentialDecryptionError("credentials cannot be decrypted")


CIPHER = PlainCipher()


def _stored(secret) -> bytes:
    return CIPHER.encrypt(json.dumps(secret))


def test_no_auth_needs_nothing():
    result = read_credentials("NoAuth", None, None)
    assert result.state == "not_required" and result.usable
    assert isinstance(result.auth, NoAuthConfig)


@pytest.mark.parametrize(
    "auth_type, secret, config",
    [
        ("BasicAuth", {"user": "u", "password": "p"}, BasicAuthConfig),
        ("BearerToken", {"token": "t"}, BearerTokenAuthConfig),
        ("ApiKey", {"key_name": "X-Key", "key_value": "k"}, ApiKeyAuthConfig),
    ],
)
def test_usable_credentials_become_an_auth_config(auth_type, secret, config):
    result = read_credentials(auth_type, _stored(secret), CIPHER)
    assert result.state == "ok" and result.usable and result.detail is None
    assert isinstance(result.auth, config)


def test_missing_credentials():
    result = read_credentials("BasicAuth", None, CIPHER)
    assert result.state == "missing" and not result.usable and result.auth is None
    assert "no credentials stored" in result.detail


def test_missing_key():
    stored = _stored({"user": "u", "password": "p"})
    result = read_credentials("BasicAuth", stored, None)
    assert result.state == "missing_key" and not result.usable
    assert "UMP_REGISTRY_CREDENTIALS_KEY" in result.detail


def test_key_that_cannot_decrypt():
    stored = _stored({"user": "u", "password": "p"})
    result = read_credentials("BasicAuth", stored, WrongKeyCipher())
    assert result.state == "undecryptable" and not result.usable
    assert result.auth is None


@pytest.mark.parametrize(
    "plaintext",
    [
        "not json",
        json.dumps({"token": "t"}),  # fields of another auth type
        json.dumps(["user", "password"]),  # not an object
    ],
)
def test_decrypted_but_invalid_for_the_auth_type(plaintext):
    result = read_credentials("BasicAuth", CIPHER.encrypt(plaintext), CIPHER)
    assert result.state == "invalid" and not result.usable
    assert result.auth is None and "BasicAuth" in result.detail
