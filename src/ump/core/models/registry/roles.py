"""Platform roles (Keycloak realm roles, REF-F11) used by the registry.

Model-access roles (``{provider}``, ``{provider}:{process}``) are not listed
here; they are derived from published models (see ``model_access_roles``).
"""

from __future__ import annotations

from typing import Iterable

PROVIDER = "provider"
VERIFIER = "verifier"
USER = "user"
VIEWER = "viewer"
ACCESS_ADMIN = "access-admin"
PLATFORM_ADMIN = "platform-admin"

PLATFORM_ROLES = frozenset(
    {PROVIDER, VERIFIER, USER, VIEWER, ACCESS_ADMIN, PLATFORM_ADMIN}
)

# Roles that may browse the catalogue when UMP_PUBLIC_PROCESSES=false (D10).
# `user` is a composite of `viewer` in Keycloak; both are accepted so the rule
# does not depend on how the realm expands composites into the token.
CATALOGUE_ROLES = frozenset({VIEWER, USER})


def model_access_roles(published: Iterable[tuple[str, str]]) -> list[str]:
    """Role names for (server name, remote process id) pairs, sorted, unique.

    One ``{provider}`` role per server and one ``{provider}:{process}`` per model,
    exactly the names AuthorizationService checks on execution.
    """
    from ump.core.models.providers_config import canonical_process_id

    names: set[str] = set()
    for server, process in published:
        names.add(server)
        names.add(canonical_process_id(server, process))
    return sorted(names)
