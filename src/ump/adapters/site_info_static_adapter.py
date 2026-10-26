from ump.core.interfaces.site_info import SiteInfoPort
from ump.core.settings import app_settings
from ump.core.utils.api_paths import external_base


class StaticSiteInfoAdapter(SiteInfoPort):
    def get_site_info(self):
        base = app_settings.UMP_API_SERVER_URL_PREFIX.rstrip("/") or ""
        routes = []
        # Provide routes for each supported API version
        for ver in getattr(app_settings, "UMP_SUPPORTED_API_VERSIONS", ["1.0"]):
            prefix = f"{base}/v{ver}"
            routes.append(
                {
                    "path": f"{prefix}/processes",
                    "description": f"List available processes (v{ver})",
                }
            )
            routes.append(
                {
                    "path": f"{prefix}/jobs",
                    "description": f"List submitted jobs (v{ver})",
                }
            )

        # OpenAPI and the docs UI are served by FastAPI at the application root,
        # not under a version prefix, so they take the external base only.
        routes.append(
            {
                "path": f"{external_base()}/openapi.json",
                "description": "OpenAPI definition",
            }
        )

        return {
            "title": app_settings.UMP_SITE_TITLE,
            "openapi_url": f"{external_base()}/openapi.json",
            "docs_url": f"{external_base()}/docs",
            "description": app_settings.UMP_SITE_DESCRIPTION,
            "contact": app_settings.UMP_SITE_CONTACT,
            "routes": routes,
        }
