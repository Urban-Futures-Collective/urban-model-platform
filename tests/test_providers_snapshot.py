"""ProvidersSnapshot (core lookup rules shared by all ProvidersPort adapters)
and the outbound-credential mapping."""

import pytest
from pydantic import SecretStr

from ump.core.models.providers_config import (
    BasicAuthConfig,
    NoAuthConfig,
    ProcessConfig,
    ProviderConfig,
    ProvidersSnapshot,
    auth_config_from_secret,
    auth_config_to_secret,
)

SNAPSHOT = ProvidersSnapshot(
    [
        ProviderConfig.model_validate(
            {
                "name": "srv",
                "url": "http://srv.local",
                "processes": [
                    {"id": "echo"},
                    {"id": "srv:already-prefixed"},
                    {"id": "fair2adapt:flood"},  # remote is another UMP
                    {"id": "off", "exclude": True},
                    {"id": "gone", "state": "deactivated"},
                ],
            }
        )
    ]
)


def test_lookup_by_canonical_id():
    assert SNAPSHOT.get_process_config("srv", "srv:echo").id == "echo"
    assert SNAPSHOT.get_process_config("srv", "srv:already-prefixed") is not None
    assert SNAPSHOT.get_process_config("srv", "srv:fair2adapt:flood") is not None
    # the bare remote id is not a canonical id
    assert SNAPSHOT.get_process_config("srv", "echo") is None
    assert SNAPSHOT.get_process_config("other", "other:echo") is None


def test_get_processes_lists_canonical_ids_in_any_state():
    assert SNAPSHOT.get_processes("srv") == [
        "srv:echo",
        "srv:already-prefixed",
        "srv:fair2adapt:flood",
        "srv:off",
        "srv:gone",
    ]
    assert SNAPSHOT.get_processes("other") == []


@pytest.mark.parametrize(
    "process_id, available",
    [("srv:echo", True), ("srv:off", False), ("srv:gone", False), ("srv:x", False)],
)
def test_availability(process_id, available):
    assert SNAPSHOT.check_process_availability("srv", process_id) is available


def test_provider_lookup_and_names():
    assert SNAPSHOT.provider_names() == ["srv"]
    assert SNAPSHOT.get_provider("srv").name == "srv"
    assert SNAPSHOT.get_provider("nope") is None
    assert ProvidersSnapshot().providers == []


def test_state_defaults_to_published():
    assert ProcessConfig(id="x").state == "published"


@pytest.mark.parametrize(
    "auth",
    [
        BasicAuthConfig(type="BasicAuth", user="u", password=SecretStr("p")),
        {"type": "ApiKey", "key_name": "X-Key", "key_value": "k"},
        {"type": "BearerToken", "token": "t"},
    ],
)
def test_credentials_round_trip(auth):
    original = (
        auth_config_from_secret(auth["type"], auth) if isinstance(auth, dict) else auth
    )
    auth_type, secret = auth_config_to_secret(original)
    assert all(isinstance(v, str) for v in secret.values())
    assert auth_config_from_secret(auth_type, secret) == original


def test_no_auth_has_no_secret():
    assert auth_config_to_secret(NoAuthConfig()) == ("NoAuth", None)
    assert isinstance(auth_config_from_secret("NoAuth", None), NoAuthConfig)
