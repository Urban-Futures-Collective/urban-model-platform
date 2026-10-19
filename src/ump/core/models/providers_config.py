from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    field_validator,
)

# Execution state of a process (REF-F12 D7, D9). Processes from providers.yaml
# are always "published"; the registry derives it from the model status.
ProcessState = Literal["published", "in_review", "deactivated"]


class GraphProperties(BaseModel):
    """Properties for graph-based result visualization"""

    root_path: str = Field(
        alias="root-path",
        description=(
            "If the results are stored in Geoserver, "
            "you can specify the object path to the "
            "feature collection using root-path. "
            "Use dots to separate a path with several "
            "components: root-path: result.some_obj.some_features."
        ),
    )
    x_path: str = Field(
        alias="x-path",
        description=(
            "Object path to the x-coordinate field. "
            "Use dots to separate path components."
        ),
    )
    y_path: str = Field(
        alias="y-path",
        description=(
            "Object path to the y-coordinate field. "
            "Use dots to separate path components."
        ),
    )


class ProcessConfig(BaseModel):
    """Configuration for an individual process"""

    id: str = Field(description="The unique identifier for this process")
    description: str | None = None
    version: str | None = None
    result_storage: Literal["geoserver", "remote"] = Field(
        default="remote", alias="result-storage"
    )
    exclude: bool = False
    result_path: str | None = Field(
        default=None,
        alias="result-path",
        description=(
            "If the results should be stored in Geoserver, "
            "you can specify the object path to the "
            "feature collection using result-path. "
            "Use dots to separate a path with several "
            "components: result-path: result.some_obj.some_features."
        ),
    )
    graph_properties: GraphProperties | None = Field(
        default=None,
        alias="graph-properties",
        description=(
            "If the results are stored in Geoserver, "
            "you can specify the graph properties using "
            "graph-properties."
        ),
    )
    ttw_job_done: float | None = Field(
        default=None,
        alias="ttw-job-done",
        description=(
            "Time to wait (in seconds) until remote job is finished. "
            "If not set, falls back to provider-level ttw_job_done. "
            "Allows per-process fine-tuning for processes with different "
            "computational footprints on the same provider."
        ),
    )
    poll_interval: float | None = Field(
        default=None,
        alias="poll-interval",
        description=(
            "Interval in seconds between remote status polling requests. "
            "If not set, falls back to JobManagerConfig default. "
            "Allows per-process tuning for different computational characteristics."
        ),
    )
    anonymous_access: bool = Field(
        default=False,
        alias="anonymous-access",
        description=(
            "If set to True, the process can be seen and run "
            "by anonymous users. Jobs and layers created "
            "by anonymous users will be cleaned up after some time."
        ),
    )
    deterministic: bool = Field(
        default=False,
        description=(
            "If set to True, the process is regarded deterministic. "
            "This means that such a process will always produce "
            "the same result for the same input. So, outputs can be "
            "cached based on inputs"
        ),
    )
    state: ProcessState = Field(
        default="published",
        description=(
            "Execution state. 'published': normal access rules; 'in_review': "
            "only verifiers may run it; 'deactivated': nobody may start a run, "
            "but running jobs can still be polled and their results fetched."
        ),
    )


class BasicAuthConfig(BaseModel):
    type: Literal["BasicAuth"]
    user: str
    password: SecretStr


class ApiKeyAuthConfig(BaseModel):
    type: Literal["ApiKey"]
    key_name: str
    key_value: SecretStr


class BearerTokenAuthConfig(BaseModel):
    type: Literal["BearerToken"]
    token: SecretStr


class NoAuthConfig(BaseModel):
    type: Literal["NoAuth"] = "NoAuth"


AuthConfig = BasicAuthConfig | ApiKeyAuthConfig | BearerTokenAuthConfig | NoAuthConfig


class ProviderConfig(BaseModel):
    """Configuration for a single provider"""

    name: str = Field(description="The name of the provider (e.g., 'infrared')")
    url: HttpUrl = Field(
        description=(
            "The URL of the model server pointing to an OGC Processes API. "
            "It should be a valid HTTP or HTTPS URL with path to the landing page."
        )
    )
    ttw_job_done: float = Field(
        default=1500.0,
        alias="ttw-job-done",
        description=(
            "Time to wait (in seconds) until remote job is finished. "
            "Serves as the provider-wide default when ProcessConfig.ttw_job_done "
            "is not set. Allows per-provider customization of timeout behavior."
        ),
    )
    authentication: AuthConfig = Field(
        default_factory=NoAuthConfig,
        description="Authentication configuration for this provider",
    )
    processes: list[ProcessConfig] = Field(
        default_factory=list,
        description="List of processes available from this provider",
    )

    @field_validator("url", mode="before")
    def ensure_trailing_slash(cls, value: str) -> HttpUrl:
        """Ensure url has a trailing slash."""
        if not str(value).endswith("/"):
            value += "/"
        return HttpUrl(value)


class ProvidersConfig(BaseModel):
    """Root configuration containing all providers"""

    providers: list[ProviderConfig] = Field(
        description="List of provider configurations"
    )


# ---------------------------------------------------------------------------
# Outbound credentials <-> stored secret
# ---------------------------------------------------------------------------

_AUTH_CONFIG = TypeAdapter(AuthConfig)


def auth_config_to_secret(auth: AuthConfig) -> tuple[str, dict[str, str] | None]:
    """Split *auth* into its type and the secret fields to store encrypted.

    Returns ``(auth_type, None)`` for ``NoAuth``.
    """
    if isinstance(auth, NoAuthConfig):
        return auth.type, None
    secret = {
        key: value.get_secret_value() if isinstance(value, SecretStr) else value
        for key, value in auth
        if key != "type"
    }
    return auth.type, secret


def auth_config_from_secret(
    auth_type: str, secret: dict[str, Any] | None
) -> AuthConfig:
    """Inverse of ``auth_config_to_secret``."""
    return _AUTH_CONFIG.validate_python({**(secret or {}), "type": auth_type})


# ---------------------------------------------------------------------------
# Lookup rules shared by every ProvidersPort implementation
# ---------------------------------------------------------------------------


def canonical_process_id(provider_name: str, configured_id: str) -> str:
    """UMP-local process id for a configured (remote) id.

    A configured id that already carries the provider prefix (the remote is
    another UMP instance) is used as-is to avoid double-prefixing.
    """
    if configured_id.startswith(f"{provider_name}:"):
        return configured_id
    return f"{provider_name}:{configured_id}"


class ProvidersSnapshot:
    """Immutable, indexed view of a provider configuration.

    ``ProvidersPort`` adapters build a new snapshot on every (re)load and swap
    it in whole, so readers never see a half-updated configuration.
    """

    def __init__(self, providers: list[ProviderConfig] | None = None) -> None:
        self._providers = tuple(providers or ())
        self._by_name = {p.name: p for p in self._providers}

    @property
    def providers(self) -> list[ProviderConfig]:
        return list(self._providers)

    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    def get_provider(self, provider_name: str) -> ProviderConfig | None:
        return self._by_name.get(provider_name)

    def get_process_config(
        self, provider_name: str, process_id: str
    ) -> ProcessConfig | None:
        """Configured process for a canonical id, in any state."""
        provider = self._by_name.get(provider_name)
        if provider is None:
            return None
        for process in provider.processes:
            if canonical_process_id(provider_name, process.id) == process_id:
                return process
        return None

    def get_processes(self, provider_name: str) -> list[str]:
        provider = self._by_name.get(provider_name)
        if provider is None:
            return []
        return [canonical_process_id(provider_name, p.id) for p in provider.processes]

    def check_process_availability(self, provider_name: str, process_id: str) -> bool:
        """True if the process is configured, not excluded and not deactivated."""
        process = self.get_process_config(provider_name, process_id)
        return (
            process is not None
            and not process.exclude
            and process.state != "deactivated"
        )
