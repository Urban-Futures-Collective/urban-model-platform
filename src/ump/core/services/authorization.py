"""AuthorizationService — decides whether a caller may execute a process.

This is pure business logic and lives in the core: it depends only on
``ProvidersPort`` (to read anonymous-access configuration) and on the
``AuthContext`` produced by the auth adapter.  The web adapter merely calls
``check_process_access`` and lets the raised ``OGCProcessException`` propagate
to the global error handler.

Authorization rules (role-based, two levels), by process state (REF-F12 D7, D9):
- ``deactivated``: nobody may run it — checked before the anonymous-access
  shortcut — and it is not listed.
- ``in_review``: only holders of the ``verifier`` platform role may see and run
  it; ``anonymous-access`` and model-access roles do not apply.
- ``published`` (every process from providers.yaml):
  - a process configured with ``anonymous-access: true`` is executable without
    authentication;
  - a ``{provider_name}`` role grants access to every process of that provider;
  - a ``{provider_name}:{process_bare_id}`` role grants access to one process;
  - the ``verifier`` role grants access as well.
"""

from __future__ import annotations

import logging
from typing import Optional

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.providers import ProvidersPort
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.providers_config import ProcessConfig, canonical_process_id
from ump.core.models.registry import roles

_log = logging.getLogger(__name__)

# Platform role of the people who review models (REF-F11/F12).
VERIFIER_ROLE = roles.VERIFIER


class AuthorizationService:
    """Access-control decisions for process execution.

    Stateless apart from the injected ``ProvidersPort``. Safe to share a single
    instance across requests.
    """

    def __init__(self, providers: ProvidersPort) -> None:
        self._providers = providers

    def can_access_process(self, auth: AuthContext, process_id: str) -> bool:
        """Whether *auth* may use *process_id*, as a plain predicate.

        This is the single implementation of the access rules. Callers that need
        to *enforce* access use ``check_process_access``; callers that need to
        *filter* a list (the MCP tool catalog) use this directly, so that the
        role model is never duplicated — see reports/REF-F10-mcp-endpoint.md.
        """
        process = self._find_process(process_id)
        state = process.state if process else "published"
        if state == "deactivated":
            return False
        is_verifier = auth.is_authenticated and VERIFIER_ROLE in auth.roles
        if state == "in_review":
            return is_verifier

        if process is not None and process.anonymous_access:
            return True

        if not auth.is_authenticated:
            return False

        provider_name = self._provider_of(process_id)
        return provider_name in auth.roles or process_id in auth.roles or is_verifier

    def can_list_process(self, auth: AuthContext, process_id: str) -> bool:
        """Whether *process_id* may appear in listings / descriptions for *auth*.

        Independent of ``UMP_PUBLIC_PROCESSES``: deactivated processes are never
        shown, processes under review only to verifiers.
        """
        process = self._find_process(process_id)
        if process is None or process.state == "published":
            return True
        if process.state == "in_review":
            return auth.is_authenticated and VERIFIER_ROLE in auth.roles
        return False

    def check_process_access(self, auth: AuthContext, process_id: str) -> None:
        """Allow or deny execution of *process_id* for *auth*.

        Returns ``None`` when access is granted; raises ``OGCProcessException``
        with status 401 (unauthenticated) or 403 (authenticated but lacking the
        required role) otherwise.
        """
        process = self._find_process(process_id)
        if process is not None and process.state == "deactivated":
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Model deactivated",
                    status=403,
                    detail=f"Process '{process_id}' is deactivated and cannot be run.",
                    instance=None,
                )
            )

        if self.can_access_process(auth, process_id):
            return

        if not auth.is_authenticated:
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Unauthorized",
                    status=401,
                    detail="Authentication required to execute this process.",
                    instance=None,
                )
            )

        provider_name = self._provider_of(process_id)
        detail = (
            f"Process '{process_id}' is under review; only role '{VERIFIER_ROLE}' "
            "may run it."
            if process is not None and process.state == "in_review"
            else f"Missing role '{provider_name}' or '{process_id}'."
        )
        raise OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Forbidden",
                status=403,
                detail=detail,
                instance=None,
            )
        )

    def require_platform_role(self, auth: AuthContext, *allowed: str) -> None:
        """Raise 401 if *auth* is anonymous, 403 if it holds none of *allowed*.

        Platform roles (REF-F11) gate the registry API; model-access roles are
        never accepted here.
        """
        if not auth.is_authenticated:
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Unauthorized",
                    status=401,
                    detail="Authentication required.",
                    instance=None,
                )
            )
        if not set(allowed) & set(auth.roles):
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Forbidden",
                    status=403,
                    detail="Requires one of the roles: " + ", ".join(sorted(allowed)),
                    instance=None,
                )
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_process(self, process_id: str) -> Optional[ProcessConfig]:
        """Configured process for *process_id* (canonical or verbatim remote id)."""
        provider_name = self._provider_of(process_id)
        if not provider_name:
            return None
        try:
            provider = self._providers.get_provider(provider_name)
        except Exception as exc:
            _log.warning(
                "[authz] provider lookup failed for %r; treating process as "
                "unconfigured: %s",
                provider_name,
                exc,
            )
            return None
        if not provider:
            return None
        for proc_cfg in provider.processes:
            canonical = canonical_process_id(provider_name, proc_cfg.id)
            if canonical == process_id or proc_cfg.id == process_id:
                return proc_cfg
        return None

    @staticmethod
    def _provider_of(process_id: str) -> Optional[str]:
        return process_id.split(":", 1)[0] if ":" in process_id else None
