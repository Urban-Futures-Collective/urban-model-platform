"""Builders shared by the composition roots (``asgi.py`` and the CLI commands).

Each entry point still wires its own object graph; these helpers only turn
settings into infrastructure so both do it the same way.
"""

from ump.core.interfaces.credential_cipher import CredentialCipherPort
from ump.core.settings import app_settings


def construct_database_url() -> str:
    if app_settings.UMP_DATABASE_URL:
        return app_settings.UMP_DATABASE_URL

    if not (app_settings.UMP_DATABASE_HOST and app_settings.UMP_DATABASE_NAME):

        raise RuntimeError(
            "UMP_DATABASE_HOST and UMP_DATABASE_NAME "
            "must be set if UMP_DATABASE_URL is not provided."
        )

    user: str = app_settings.UMP_DATABASE_USER
    password: str = (
        app_settings.UMP_DATABASE_PASSWORD.get_secret_value()
        if app_settings.UMP_DATABASE_PASSWORD
        else ""
    )
    host: str = app_settings.UMP_DATABASE_HOST
    port: int = app_settings.UMP_DATABASE_PORT
    name: str = app_settings.UMP_DATABASE_NAME

    return f"postgresql+asyncpg://{user}:{password}" f"@{host}:{port}/{name}"


def build_credential_cipher() -> CredentialCipherPort | None:
    """Cipher for model-server credentials in the registry, if a key is set."""
    key = app_settings.UMP_REGISTRY_CREDENTIALS_KEY
    if key is None:
        return None
    from ump.adapters.credential_cipher_fernet import FernetCredentialCipher

    return FernetCredentialCipher(key.get_secret_value().split(","))
