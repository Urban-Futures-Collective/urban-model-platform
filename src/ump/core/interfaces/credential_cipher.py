"""CredentialCipherPort — encrypts secrets before they are stored (REF-F12).

Used for the outbound credentials of model servers
(``registry.provider_server.credentials``). The core only sees plaintext
strings and opaque ciphertext bytes; key handling and the algorithm are the
adapter's concern.
"""

from abc import ABC, abstractmethod


class CredentialDecryptionError(Exception):
    """Ciphertext cannot be decrypted (wrong key, tampered or corrupt data)."""


class CredentialCipherPort(ABC):
    @abstractmethod
    def encrypt(self, plaintext: str) -> bytes:
        """Return authenticated ciphertext for *plaintext*."""

    @abstractmethod
    def decrypt(self, ciphertext: bytes) -> str:
        """Return the plaintext; raise ``CredentialDecryptionError`` on failure."""
