"""Helpers shared by the registry services."""

from __future__ import annotations

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.registry import ProviderServer, roles
from ump.core.models.registry.etag import etag_for


def problem(status: int, title: str, detail: str) -> OGCProcessException:
    return OGCProcessException(
        OGCExceptionResponse(
            type="about:blank", title=title, status=status, detail=detail
        )
    )


def is_admin(auth: AuthContext) -> bool:
    return auth.is_authenticated and roles.PLATFORM_ADMIN in auth.roles


def owns_server(auth: AuthContext, server: ProviderServer) -> bool:
    return is_admin(auth) or (
        auth.is_authenticated and server.owner_sub == auth.user_id
    )


def check_if_match(if_match: str | None, current_updated_at) -> None:
    """428 without If-Match, 412 if it does not name the current version."""
    if if_match is None:
        raise problem(
            428, "Precondition Required", "Send If-Match with the ETag you edited."
        )
    # "*" is deliberately not accepted: it would switch the check off
    if if_match.strip() != etag_for(current_updated_at):
        raise problem(
            412,
            "Precondition Failed",
            "The resource was changed since it was read; reload and retry.",
        )
