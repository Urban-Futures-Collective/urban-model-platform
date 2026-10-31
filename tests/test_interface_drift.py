"""Interface drift, policy A (REF-F12 phase D): diff summary, InterfaceDriftService
and InterfaceCheckScheduler. In-memory registry, in-test SafeFetchPort and lock."""

import asyncio
import copy
from datetime import datetime, timezone

from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
from ump.core.interfaces.model_registry import InterfaceCheck
from ump.core.interfaces.poll_lock import PollLockPort
from ump.core.interfaces.safe_fetch import (
    RemoteFetchError,
    SafeFetchPort,
    UnsafeUrlError,
)
from ump.core.models.registry import (
    AppUser,
    ProviderServer,
    RegistryModel,
    interface_sha256,
)
from ump.core.models.registry.audit import AuditEvent
from ump.core.models.registry.interface_diff import interface_diff
from ump.core.services.authorization import AuthorizationService
from ump.core.services.interface_drift import (
    INTERFACE_CHECK_LOCK_KEY,
    InterfaceCheckScheduler,
    InterfaceDriftService,
)
from ump.core.services.registry_catalog import RegistryCatalogService

BASE = "https://srv.example.org/"
SEED = AuditEvent(actor_sub=None, action="test.seed")
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
DESCRIPTION = {
    "id": "echo",
    "title": "Echo",
    "inputs": {"x": {"title": "X", "schema": {"type": "number"}}},
    "outputs": {"y": {"schema": {"type": "number"}}},
    "jobControlOptions": ["async-execute"],
}


# --- interface_diff ----------------------------------------------------------


def test_diff_ignores_text_changes():
    edited = copy.deepcopy(DESCRIPTION)
    edited["title"] = "Echo 2"
    edited["inputs"]["x"]["title"] = "Ex"
    edited["inputs"]["x"]["description"] = "new text"
    assert interface_diff(DESCRIPTION, edited) == {}


def test_diff_reports_added_removed_changed_and_modes():
    changed = copy.deepcopy(DESCRIPTION)
    changed["inputs"]["x"]["schema"]["type"] = "string"
    changed["inputs"]["z"] = {"schema": {"type": "boolean"}}
    del changed["outputs"]["y"]
    changed["jobControlOptions"] = ["sync-execute"]
    assert interface_diff(DESCRIPTION, changed) == {
        "inputs": {"added": ["z"], "removed": [], "changed": ["x"]},
        "outputs": {"added": [], "removed": ["y"], "changed": []},
        "jobControlOptions": {
            "before": ["async-execute"],
            "after": ["sync-execute"],
        },
    }


# --- InterfaceDriftService -----------------------------------------------------


class Server(SafeFetchPort):
    """Answers with ``description``, or raises ``error`` if set."""

    def __init__(self):
        self.description = copy.deepcopy(DESCRIPTION)
        self.error: Exception | None = None
        self.calls = 0

    def check_url(self, url):
        pass

    async def get_json(self, url, headers=None):
        self.calls += 1
        assert url == BASE + "processes/echo"
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.description)


class Env:
    def __init__(self, verified=True, status="published", active=True):
        self.repo = InMemoryRegistryRepository()
        self.server = Server()
        authz = AuthorizationService(None)
        self.drift = InterfaceDriftService(
            self.repo,
            catalog=RegistryCatalogService(self.repo, authz, True),
            authorization=authz,
            fetch=self.server,
            remote_auth=RemoteAuthAdapter(),
            cipher=None,
            clock=lambda: T0,
        )
        self.model = asyncio.run(self._seed(verified, status, active))

    async def _seed(self, verified, status, active):
        await self.repo.upsert_user(AppUser(sub="owner-1"))
        server = await self.repo.create_provider_server(
            ProviderServer(
                name="srv", base_url=BASE, owner_sub="owner-1", is_active=active
            ),
            audit=SEED,
        )
        sha = interface_sha256(DESCRIPTION)
        (model,) = await self.repo.create_models(
            [
                RegistryModel(
                    slug="srv-echo",
                    provider_server_id=server.id,
                    remote_process_id="echo",
                    owner_sub="owner-1",
                    full_name={"en": "Echo"},
                    status=status,
                    verified_interface=DESCRIPTION if verified else None,
                    verified_interface_sha256=sha if verified else None,
                    current_interface_sha256=sha,
                    interface_state="in_sync" if verified else "unchecked",
                )
            ],
            audit=[SEED],
        )
        self.repo.audit.clear()
        return model

    def check(self):
        return asyncio.run(self.drift.check_all())

    def stored(self) -> RegistryModel:
        return next(iter(self.repo.models.values()))

    def actions(self):
        return [e.action for e in self.repo.audit]


def test_in_sync_drifted_restored():
    env = Env()
    (result,) = env.check()
    assert result.interface_state == "in_sync"
    assert env.actions() == []  # no state change, no event
    assert env.stored().last_checked_at == T0

    env.server.description["inputs"]["x"]["schema"]["type"] = "string"
    (result,) = env.check()
    assert (result.previous_state, result.interface_state) == ("in_sync", "drifted")
    event = env.repo.audit[-1]
    assert event.action == "model.interface.drifted"
    assert event.actor_sub is None and event.model_id == env.model.id
    assert event.details["diff"] == {
        "inputs": {"added": [], "removed": [], "changed": ["x"]}
    }
    stored = env.stored()
    assert stored.interface_state == "drifted"
    assert stored.current_interface_sha256 == interface_sha256(env.server.description)
    assert stored.verified_interface_sha256 == interface_sha256(DESCRIPTION)

    env.check()  # still drifted, same hash: nothing new
    assert env.actions() == ["model.interface.drifted"]

    env.server.description["inputs"]["w"] = {"schema": {"type": "number"}}
    env.check()  # drifted further: recorded again
    assert env.actions() == ["model.interface.drifted"] * 2

    env.server.description = copy.deepcopy(DESCRIPTION)
    (result,) = env.check()
    assert result.interface_state == "in_sync"
    assert env.actions()[-1] == "model.interface.restored"
    assert env.repo.audit[-1].details["previous"] == "drifted"


def test_check_does_not_change_the_etag():
    env = Env()
    before = env.stored().updated_at
    env.server.description["outputs"] = {}
    env.check()
    assert env.stored().interface_state == "drifted"
    assert env.stored().updated_at == before


def test_unreachable_recorded_once_and_keeps_current_hash():
    env = Env()
    for error in (RemoteFetchError("HTTP 503"), UnsafeUrlError("private address")):
        env.server.error = error
        (result,) = env.check()
        assert result.interface_state == "unreachable"
        assert result.error
    assert env.actions() == ["model.interface.unreachable"]
    assert env.stored().current_interface_sha256 == interface_sha256(DESCRIPTION)

    env.server.error = None
    env.check()
    assert env.actions()[-1] == "model.interface.restored"
    assert env.stored().interface_state == "in_sync"


def test_non_object_description_is_unreachable():
    env = Env()
    env.server.description = ["not", "an", "object"]
    (result,) = env.check()
    assert result.interface_state == "unreachable"


def test_unverified_model_stays_in_sync_but_change_is_recorded():
    env = Env(verified=False, status="in_review")
    (result,) = env.check()
    assert result.interface_state == "in_sync"
    assert env.actions() == []

    env.server.description["outputs"] = {}
    (result,) = env.check()
    assert result.interface_state == "in_sync"
    assert env.actions() == ["model.interface.changed"]
    assert env.stored().current_interface_sha256 == interface_sha256(
        env.server.description
    )


def test_only_models_known_to_execution_on_active_servers_are_checked():
    assert Env(status="draft").check() == []
    assert Env(status="submitted").check() == []
    assert Env(active=False).check() == []
    assert len(Env(status="deactivated").check()) == 1


def test_check_skipped_if_reverified_meanwhile():
    env = Env()
    env.server.description["outputs"] = {}
    # a re-verification lands after the check read the model
    applied = asyncio.run(
        env.repo.record_interface_checks(
            [
                InterfaceCheck(
                    model_id=env.model.id,
                    expected_verified_sha256="stale-hash",
                    interface_state="drifted",
                    current_interface_sha256="x",
                    last_checked_at=T0,
                    audit=AuditEvent(actor_sub=None, action="model.interface.drifted"),
                )
            ]
        )
    )
    assert applied == set()
    assert env.stored().interface_state == "in_sync"
    assert env.actions() == []


# --- InterfaceCheckScheduler ---------------------------------------------------


class Lock(PollLockPort):
    def __init__(self, free=True):
        self.free = free
        self.keys: list[str] = []
        self.released: list[str] = []

    async def try_acquire(self, job_id):
        self.keys.append(job_id)
        return self.free

    async def release(self, job_id):
        self.released.append(job_id)


def test_scheduler_runs_only_as_leader_and_keeps_the_lock():
    env = Env()

    async def scenario():
        other = Lock(free=False)
        follower = InterfaceCheckScheduler(env.drift, other, interval=3600)
        assert await follower.run_once() is False
        assert env.server.calls == 0

        lock = Lock()
        leader = InterfaceCheckScheduler(env.drift, lock, interval=3600)
        assert await leader.run_once() is True
        assert await leader.run_once() is True
        assert env.server.calls == 2
        assert lock.keys == [INTERFACE_CHECK_LOCK_KEY]  # acquired once, kept
        await leader.__aexit__(None, None, None)
        assert lock.released == [INTERFACE_CHECK_LOCK_KEY]

    asyncio.run(scenario())


def test_scheduler_survives_a_failing_check():
    class Broken:
        async def check_all(self):
            raise RuntimeError("database down")

    async def scenario():
        scheduler = InterfaceCheckScheduler(Broken(), Lock(), interval=3600)
        assert await scheduler.run_once() is True

    asyncio.run(scenario())


def test_scheduler_lifecycle_starts_and_stops_the_task():
    env = Env()

    async def scenario():
        lock = Lock()
        async with InterfaceCheckScheduler(
            env.drift, lock, interval=0.01, first_delay=0
        ):
            for _ in range(100):
                if env.server.calls:
                    break
                await asyncio.sleep(0.01)
        assert env.server.calls >= 1
        assert lock.released == [INTERFACE_CHECK_LOCK_KEY]

    asyncio.run(scenario())
