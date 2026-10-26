"""Fernet implementation of CredentialCipherPort.

Fernet is authenticated encryption (AES-128-CBC + HMAC-SHA256). Several keys
can be given for rotation: the first one encrypts, all of them are tried for
decryption (``MultiFernet``). Each Fernet token already identifies its key by
failing authentication under any other key, so no explicit key-id prefix is
needed. Generate a key with ``Fernet.generate_key()``.
"""

from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from ump.core.interfaces.credential_cipher import (
    CredentialCipherPort,
    CredentialDecryptionError,
)


class FernetCredentialCipher(CredentialCipherPort):
    def __init__(self, keys: Sequence[str]) -> None:
        if not keys:
            raise ValueError("FernetCredentialCipher needs at least one key")
        self._fernet = MultiFernet([Fernet(key.strip()) for key in keys])

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(ciphertext)).decode("utf-8")
        except InvalidToken as exc:
            raise CredentialDecryptionError("credentials cannot be decrypted") from exc
