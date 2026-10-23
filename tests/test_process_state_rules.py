"""Process state rules (REF-F12 D7, D9) and prerequisite P1.

- P1: executing a process that is not configured or is excluded → 404, even
  for a holder of the provider role.
- deactivated: refused for everyone (403 "Model deactivated"), also for
  anonymous-access processes; hidden from listings; still resolvable through
  ProvidersPort so running jobs keep polling.
- in_review: only the ``verifier`` role may run and see it.
- published: unchanged rules, plus ``verifier``.

Providers come from an in-test ProvidersPort over the core ProvidersSnapshot.
"""

from typing import List

import pytest

from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.providers import ProvidersPort
from ump.core.managers.job_manager import JobExecutionContext
from ump.core.managers.steps.execution_steps import ValidateAndResolveStep
from ump.core.models.providers_config import ProviderConfig, ProvidersSnapshot
from ump.core.services.authorization import AuthorizationService


class SnapshotProviders(ProvidersPort):
    def __init__(self, providers: List[ProviderConfig]) -> None:
        self._snapshot = ProvidersSnapshot(providers)

    def load_providers(self) -> None:
        pass

    def get_providers(self):
        return self._snapshot.providers

    def get_provider(self, provider_name):
        return self._snapshot.get_provider(provider_name)

    def get_process_config(self, provider_name, process_id):
        return self._snapshot.get_process_config(provider_name, process_id)

    def list_providers(self):
        return self._snapshot.provider_names()

    def get_processes(self, provider_name):
        return self._snapshot.get_processes(provider_name)

    def check_process_availability(self, provider_name, process_id):
        return self._snapshot.check_process_availability(provider_name, process_id)


PROVIDERS = SnapshotProviders(
    [
        ProviderConfig.model_validate(
            {
                "name": "srv",
                "url": "http://srv.local/",
                "processes": [
                    {"id": "live"},
                    {"id": "open", "anonymous-access": True},
                    {"id": "hidden", "exclude": True},
                    {"id": "review", "state": "in_review"},
                    {"id": "gone", "state": "deactivated", "anonymous-access": True},
                ],
            }
        )
    ]
)
AUTHZ = AuthorizationService(PROVIDERS)

ANON = AuthContext(user_id=None, roles=[], is_authenticated=False)
PROVIDER_ROLE = AuthContext(user_id="u", roles=["srv"], is_authenticated=True)
VERIFIER = AuthContext(user_id="v", roles=["verifier"], is_authenticated=True)
NOBODY = AuthContext(user_id="n", roles=[], is_authenticated=True)


async def _resolve(process_id: str) -> JobExecutionContext:
    context = JobExecutionContext(process_id=process_id)
    await ValidateAndResolveStep(ColonProcessId(), PROVIDERS).process(context)
    return context


# --- P1 and deactivation in the execution pipeline ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("process_id", ["srv:hidden", "srv:not-configured"])
async def test_unconfigured_or_excluded_process_is_not_executable(process_id):
    # The pipeline refuses it independent of the caller's roles, so the
    # provider role (which AuthorizationService still accepts) cannot open it.
    context = await _resolve(process_id)
    assert context.should_halt
    assert context.response["status"] == 404


@pytest.mark.asyncio
async def test_deactivated_process_halts_pipeline_with_403():
    context = await _resolve("srv:gone")
    assert context.should_halt
    assert context.response["status"] == 403


@pytest.mark.asyncio
async def test_configured_process_resolves_to_verbatim_remote_id():
    context = await _resolve("srv:live")
    assert not context.should_halt
    assert context.provider_process_id == "live"


@pytest.mark.asyncio
async def test_bare_id_resolves_only_to_a_configured_process():
    assert (await _resolve("live")).provider_process_id == "live"
    assert (await _resolve("not-configured")).response["status"] == 404


# --- AuthorizationService ---------------------------------------------------


@pytest.mark.parametrize("auth", [ANON, PROVIDER_ROLE, VERIFIER])
def test_deactivated_is_refused_for_everyone_before_anonymous_access(auth):
    with pytest.raises(OGCProcessException) as excinfo:
        AUTHZ.check_process_access(auth, "srv:gone")
    assert excinfo.value.response.status == 403
    assert excinfo.value.response.title == "Model deactivated"


@pytest.mark.parametrize(
    "auth, allowed",
    [(ANON, False), (NOBODY, False), (PROVIDER_ROLE, False), (VERIFIER, True)],
)
def test_in_review_only_for_verifiers(auth, allowed):
    assert AUTHZ.can_access_process(auth, "srv:review") is allowed
    assert AUTHZ.can_list_process(auth, "srv:review") is allowed


def test_in_review_refusal_names_the_verifier_role():
    with pytest.raises(OGCProcessException) as excinfo:
        AUTHZ.check_process_access(PROVIDER_ROLE, "srv:review")
    assert excinfo.value.response.status == 403
    assert "verifier" in excinfo.value.response.detail


@pytest.mark.parametrize(
    "auth, allowed",
    [(ANON, False), (NOBODY, False), (PROVIDER_ROLE, True), (VERIFIER, True)],
)
def test_published_keeps_role_rules_and_admits_verifiers(auth, allowed):
    assert AUTHZ.can_access_process(auth, "srv:live") is allowed


def test_anonymous_access_still_works_for_published():
    AUTHZ.check_process_access(ANON, "srv:open")


@pytest.mark.parametrize("auth", [ANON, PROVIDER_ROLE, VERIFIER])
def test_deactivated_is_never_listed(auth):
    assert AUTHZ.can_list_process(auth, "srv:gone") is False


def test_deactivated_stays_resolvable_for_running_jobs():
    # JobManager polls and proxies results via get_provider/get_process_config
    assert PROVIDERS.get_provider("srv") is not None
    assert PROVIDERS.get_process_config("srv", "srv:gone").state == "deactivated"
    assert PROVIDERS.check_process_availability("srv", "srv:gone") is False
