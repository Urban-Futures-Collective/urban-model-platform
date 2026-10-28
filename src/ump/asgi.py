"""ASGI entry point for production deployments.

Exposes the FastAPI application at module level so uvicorn and gunicorn
can import it directly, enabling multi-worker deployments:

  # uvicorn (recommended):
  uvicorn ump.asgi:app --host 0.0.0.0 --port 8000 --workers 4

  # gunicorn with uvicorn workers:
  gunicorn -k uvicorn.workers.UvicornWorker -w 4 ump.asgi:app

Each worker process imports this module independently, so every worker
gets its own adapter instances (file watchers, HTTP clients, DB connections).
"""

import os

from sqlalchemy.ext.asyncio import create_async_engine

from ump.adapters.aiohttp_client_adapter import AioHttpClientAdapter
from ump.adapters.colon_process_id_validator import ColonProcessId
from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.adapters.jwt_auth_adapter import JwtAuthAdapter
from ump.adapters.logging_adapter import LoggingAdapter
from ump.adapters.poll_lock_noop import NoOpPollLock
from ump.adapters.provider_config_file_adapter import ProviderConfigFileAdapter
from ump.adapters.remote_auth_adapter import RemoteAuthAdapter
from ump.adapters.retry_tenacity import TenacityRetryAdapter
from ump.adapters.safe_fetch_aiohttp import AioHttpSafeFetch
from ump.adapters.site_info_static_adapter import StaticSiteInfoAdapter
from ump.adapters.web.fastapi import create_app
from ump.adapters.web.registry_router import RegistryServices
from ump.core.config import JobManagerConfig
from ump.core.interfaces.audit_log import AuditLogPort
from ump.core.interfaces.credential_cipher import CredentialCipherPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.model_registry import ModelRegistryPort
from ump.core.interfaces.poll_lock import PollLockPort
from ump.core.interfaces.providers import ProvidersPort
from ump.core.logging_config import configure_logging
from ump.core.managers.job_manager import JobManager
from ump.core.managers.observers import (
    AuditObserver,
    PollingSchedulerObserver,
    ResultsVerificationObserver,
    StatusHistoryObserver,
)
from ump.core.managers.process_manager import ProcessManager
from ump.core.services.authorization import AuthorizationService
from ump.core.services.interface_drift import InterfaceCheckScheduler
from ump.core.services.tool_catalog import ToolCatalogService
from ump.core.settings import app_settings, set_logger

# ---------------------------------------------------------------------------
# Logging must be configured first — before any adapter is instantiated.
#
# Every adapter that logs at startup (e.g. ProviderConfigFileAdapter calling
# load_providers() in __init__) goes through the DelegatingLogger from
# settings.py, which starts as NoOpLogger.  Calling configure_logging() and
# set_logger() here wires the delegate to a real LoggingAdapter backed by
# Python's logging module, so startup logs are visible from the very first
# line of adapter code.
# ---------------------------------------------------------------------------
configure_logging(app_settings.UMP_LOG_LEVEL)
set_logger(LoggingAdapter("ump", app_settings.UMP_LOG_LEVEL))


# ---------------------------------------------------------------------------
# Factories for core managers (one set per worker process)
# ---------------------------------------------------------------------------
def construct_database_url() -> str:
    if app_settings.UMP_DATABASE_URL:
        return app_settings.UMP_DATABASE_URL
    
    if not (app_settings.UMP_DATABASE_HOST and app_settings.UMP_DATABASE_NAME):

        raise RuntimeError(
            "UMP_DATABASE_HOST and UMP_DATABASE_NAME "
            "must be set if UMP_DATABASE_URL is not provided."
        )

    user: str = app_settings.UMP_DATABASE_USER
    password: str = app_settings.UMP_DATABASE_PASSWORD.get_secret_value() if app_settings.UMP_DATABASE_PASSWORD else ""
    host: str = app_settings.UMP_DATABASE_HOST
    port: int = app_settings.UMP_DATABASE_PORT
    name: str = app_settings.UMP_DATABASE_NAME

    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{host}:{port}/{name}"
    )

def build_credential_cipher() -> CredentialCipherPort | None:
    """Cipher for model-server credentials in the registry, if a key is set."""
    key = app_settings.UMP_REGISTRY_CREDENTIALS_KEY
    if key is None:
        return None
    from ump.adapters.credential_cipher_fernet import FernetCredentialCipher

    return FernetCredentialCipher(key.get_secret_value().split(","))


def _process_manager_factory(client):
    return ProcessManager(
        providers_port,
        client,
        process_id_validator=process_id_validator,
        remote_auth=remote_auth,
    )


def _tool_catalog_factory(process_manager):
    """Core service behind GET /mcp/tools (reports/REF-F10-mcp-endpoint.md)."""
    return ToolCatalogService(
        process_manager=process_manager,
        authorization_service=authorization_service,
        process_id_validator=process_id_validator,
    )


def _job_manager_factory(client, process_manager):
    job_config = JobManagerConfig.from_app_settings(app_settings)
    retry_adapter = TenacityRetryAdapter(
        attempts=job_config.forward_max_retries,
        wait_initial=job_config.forward_retry_base_wait,
        wait_max=job_config.forward_retry_max_wait,
    )
    jm = JobManager(
        providers=providers_port,
        http_client=client,
        process_id_validator=process_id_validator,
        job_repo=job_repo,
        config=job_config,
        retry_port=retry_adapter,
        remote_auth=remote_auth,
        poll_lock=poll_lock,
        observers=[],
    )
    jm._observers = [
        StatusHistoryObserver(repository=job_repo),
        PollingSchedulerObserver(schedule_callback=jm._schedule_poll),
        ResultsVerificationObserver(http_client=client),
        AuditObserver(audit_log=audit_log),
    ]
    process_manager.attach_job_manager(jm)
    return jm

# ---------------------------------------------------------------------------
# Infrastructure adapters (one set per worker process)
# ---------------------------------------------------------------------------

# One engine (connection pool) per worker, shared by every database adapter.
_needs_database = (
    app_settings.UMP_JOB_STORE == "postgres" or app_settings.UMP_PROVIDERS_SOURCE == "db"
)
engine = create_async_engine(construct_database_url()) if _needs_database else None

# Async context managers the web lifespan keeps running while the app serves.
lifecycle: list = []

# Encrypts model-server credentials in the registry (None: no key configured).
credential_cipher = build_credential_cipher()

providers_port: ProvidersPort
if app_settings.UMP_PROVIDERS_SOURCE == "db":
    from ump.adapters.provider_config_db_adapter import ProviderConfigDbAdapter

    providers_port = ProviderConfigDbAdapter(
        engine,
        cipher=credential_cipher,
        poll_interval=app_settings.UMP_PROVIDERS_DB_POLL_INTERVAL,
    )
    lifecycle.append(providers_port)
else:
    _config_path = os.path.abspath(app_settings.UMP_PROVIDERS_FILE)
    providers_port = ProviderConfigFileAdapter(_config_path)
    providers_port.start_file_watcher()

http_client = AioHttpClientAdapter()
process_id_validator = ColonProcessId()
remote_auth = RemoteAuthAdapter()
jwt_auth = JwtAuthAdapter(app_settings)
authorization_service = AuthorizationService(providers_port)

if app_settings.UMP_JOB_STORE == "postgres":
    from ump.adapters.job_repository_sql import SQLModelJobRepository
    from ump.adapters.poll_lock_pg import PgAdvisoryPollLock

    _sql_repo = SQLModelJobRepository(engine=engine)
    job_repo: JobRepositoryPort = _sql_repo
    poll_lock = PgAdvisoryPollLock(_sql_repo._session_factory)
else:
    job_repo = InMemoryJobRepository("scratch/ump_jobs")
    poll_lock = NoOpPollLock()

# ---------------------------------------------------------------------------
# App factory (called once per worker)
# ---------------------------------------------------------------------------


# Model registry and audit log: SQL when a database is configured, else in
# memory (dev only).
drift_lock: PollLockPort
if engine is not None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ump.adapters.audit_log_sql import SQLAuditLog
    from ump.adapters.poll_lock_pg import PgAdvisoryPollLock
    from ump.adapters.registry_repository_sql import SQLModelRegistryRepository

    _registry_sessions = async_sessionmaker(engine, expire_on_commit=False)
    registry_repo: ModelRegistryPort = SQLModelRegistryRepository(_registry_sessions)
    audit_log: AuditLogPort = SQLAuditLog(_registry_sessions)
    # one worker runs the drift check, whatever the job store
    drift_lock = PgAdvisoryPollLock(_registry_sessions)
else:
    from ump.adapters.audit_log_inmemory import InMemoryAuditLog
    from ump.adapters.registry_repository_inmemory import InMemoryRegistryRepository

    _memory_audit = InMemoryAuditLog()
    audit_log = _memory_audit
    registry_repo = InMemoryRegistryRepository(audit_log=_memory_audit)
    drift_lock = NoOpPollLock()

registry_services = RegistryServices.create(
    registry_repo,
    authorization=authorization_service,
    providers=providers_port,
    fetch=AioHttpSafeFetch(
        allow_http=app_settings.UMP_REGISTRY_ALLOW_HTTP,
        allowed_private_hosts=app_settings.UMP_REGISTRY_ALLOWED_PRIVATE_HOSTS,
        timeout=app_settings.UMP_REGISTRY_FETCH_TIMEOUT,
        max_bytes=app_settings.UMP_REGISTRY_FETCH_MAX_BYTES,
    ),
    remote_auth=remote_auth,
    cipher=credential_cipher,
    languages=app_settings.UMP_REGISTRY_LANGUAGES,
    public_catalogue=app_settings.UMP_PUBLIC_PROCESSES,
    jobs=job_repo,
    audit_log=audit_log,
)

if app_settings.UMP_INTERFACE_CHECK_INTERVAL_SECONDS > 0:
    lifecycle.append(
        InterfaceCheckScheduler(
            registry_services.drift,
            lock=drift_lock,
            interval=app_settings.UMP_INTERFACE_CHECK_INTERVAL_SECONDS,
        )
    )

app = create_app(
    process_manager_factory=_process_manager_factory,
    http_client=http_client,
    job_manager_factory=_job_manager_factory,
    job_repo=job_repo,
    process_id_validator=process_id_validator,
    auth_port=jwt_auth,
    authorization_service=authorization_service,
    site_info=StaticSiteInfoAdapter(),
    tool_catalog_factory=_tool_catalog_factory,
    lifecycle=lifecycle,
    registry=registry_services,
)
