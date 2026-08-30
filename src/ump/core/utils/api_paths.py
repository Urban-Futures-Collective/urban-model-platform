"""Local API path construction.

Every link UMP hands a client — process links, job self/results links, the
Location header on job creation — has to resolve against the routes the app
actually mounts, which live under ``{UMP_API_SERVER_URL_PREFIX}/v{version}/``.

Before this helper existed the two halves were built inconsistently: process
links used the prefix without the version, job links hardcoded ``/jobs/{id}``
with neither, and the landing page used prefix *and* version. No single value of
UMP_API_SERVER_URL_PREFIX could make all three correct — setting it to "/v1.0"
fixed process links while doubling the landing page's paths to "/v1.0/v1.0/…".

``UMP_API_SERVER_URL_PREFIX`` now has one meaning everywhere: the external mount
point of the API ("/" when served at the root, "/api" when a reverse proxy
publishes it under /api). The version segment is appended here.
"""

from __future__ import annotations

from ump.core.settings import app_settings


def api_base() -> str:
    """Return the local base path for API links, e.g. ``/v1.0`` or ``/api/v1.0``.

    Uses the first configured supported version, which is the one the routes are
    mounted under first and the one clients are steered to from the landing page.
    """
    prefix = app_settings.UMP_API_SERVER_URL_PREFIX.rstrip("/")
    versions = getattr(app_settings, "UMP_SUPPORTED_API_VERSIONS", None) or ["1.0"]
    return f"{prefix}/v{versions[0]}"
