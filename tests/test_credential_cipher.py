"""FernetCredentialCipher (CredentialCipherPort adapter)."""

import pytest
from cryptography.fernet import Fernet

from ump.adapters.credential_cipher_fernet import FernetCredentialCipher
from ump.core.interfaces.credential_cipher import CredentialDecryptionError

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


def test_round_trip_and_ciphertext_hides_plaintext():
    cipher = FernetCredentialCipher([KEY_A])
    token = cipher.encrypt('{"password": "s3cret"}')
    assert b"s3cret" not in token
    assert cipher.decrypt(token) == '{"password": "s3cret"}'


def test_wrong_key_fails():
    token = FernetCredentialCipher([KEY_A]).encrypt("x")
    with pytest.raises(CredentialDecryptionError):
        FernetCredentialCipher([KEY_B]).decrypt(token)


def test_tampered_ciphertext_fails():
    cipher = FernetCredentialCipher([KEY_A])
    token = bytearray(cipher.encrypt("x"))
    token[-5] ^= 1
    with pytest.raises(CredentialDecryptionError):
        cipher.decrypt(bytes(token))


def test_rotation_new_key_first_still_decrypts_old_tokens():
    old_token = FernetCredentialCipher([KEY_A]).encrypt("old")
    rotated = FernetCredentialCipher([KEY_B, KEY_A])
    assert rotated.decrypt(old_token) == "old"
    # new tokens use the first key only
    with pytest.raises(CredentialDecryptionError):
        FernetCredentialCipher([KEY_A]).decrypt(rotated.encrypt("new"))


def test_needs_a_key():
    with pytest.raises(ValueError):
        FernetCredentialCipher([])
