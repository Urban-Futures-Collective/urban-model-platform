# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Urban Model Platform (UMP) is a Python/Flask implementation of the [OGC API
Processes](https://docs.ogc.org/is/18-062r2/18-062r2.html) standard. It acts as a
federation gateway ("system of systems"): it does not run simulation models itself,
but proxies job execution to one or more externally configured *model servers*
(also OGC API Processes-compliant), tracks job state in its own Postgres database,
and optionally persists geospatial job results into GeoServer.

## Common commands

```bash
# Environment setup (conda + poetry)
make initiate-dev              # creates ./.venv conda env, providers.yaml, .env, docker network, poetry install
poetry install                 # install/sync dependencies only

# Run the dev stack (starts api-db, geoserver-db, keycloak, kc-db, geoserver containers,
# runs DB migrations, then runs Flask locally with the reloader)
make start-dev
make start-dev-example         # same, plus an example modelserver container (needs git submodule init/update)
make restart-dev
make stop-dev
make clean-dev                 # WARNING: removes dev containers AND volumes

# Run the Flask app directly (equivalent to what make start-dev does at the end)
flask -A src/ump/main.py --debug run

# Database migrations (Alembic via Flask-Migrate)
FLASK_APP=src/ump/main.py flask db upgrade
FLASK_APP=src/ump/main.py flask db current
FLASK_APP=src/ump/main.py flask db migrate -m "message"   # generate a new migration

# Linting / formatting (see .pre-commit-config.yaml and pyproject.toml)
pre-commit run --all-files
black src
isort src
ruff check src

# Docs (Jupyter Book, optional dependency group)
poetry install --only=docs
make build-docs
make clean-docs

# Versioning (bump-my-version, keeps pyproject.toml and .env IMAGE_TAG in sync)
make bump-app-version part={major|minor|patch}
make set-app-version version=X.Y.Z
make bump-chart-version part={major|minor|patch}   # Helm chart under charts/
```

Note: there is currently no test suite in this repo despite pytest/pytest-cov/pytest-xdist
being declared as dev dependencies — don't assume `pytest` will find anything to run.

A `pre-push` git hook (`.githooks/pre-push`) runs `helm lint charts/urban-model-platform`.

## Configuration

- All runtime configuration is environment-variable driven via `UmpSettings`
  (`src/ump/config.py`, pydantic-settings), loaded from `.env`. Copy `.env.example` to `.env`
  to get started; `Makefile`/`make initiate-dev` does this automatically.
- Model servers ("providers") are configured separately in `providers.yaml` (copy from
  `providers.yaml.example`). This file is **hot-reloaded** at runtime by a watchdog
  polling observer (`src/ump/api/providers.py`) — editing it does not require an app
  restart, including when it's mounted as a k8s ConfigMap.
- Each provider entry declares its `url`, `authentication` (NoAuth/BasicAuth/ApiKey/BearerToken —
  see `src/ump/api/remote_auth.py` for the strategy pattern implementing these), a `timeout`,
  and a `processes` map. **Only processes explicitly listed under a provider in
  `providers.yaml` are exposed by UMP**, even if the remote server advertises more; each
  process entry controls `result-storage` (`remote` or `geoserver`), `exclude`,
  `anonymous-access`, and `deterministic` (enables result caching by input hash).
- Pydantic models for this config live in `src/ump/api/models/providers_config.py`.

## Architecture

**Request flow**: `src/ump/main.py` builds the `APIFlask` app, wires Postgres via
SQLAlchemy + Flask-Migrate, and registers blueprints under `src/ump/api/routes/`
(`processes`, `jobs`, `ensembles`, `users`, `health`), each mounted under
`UMP_API_SERVER_URL_PREFIX`. A `before_request` hook decodes the caller's Keycloak
JWT (if present) into `g.auth_token`; a value of `None` means anonymous access.
Route handlers are thin and delegate to the model/business-logic layer below them.

**Authorization model**: access to a process is granted if any of these hold —
the process has `anonymous-access: true`, the caller's Keycloak realm/client roles
include the bare provider name (grants access to *all* of that provider's processes),
or the roles include `{provider}_{process_id}` (grants access to that one process).
See `has_user_access_rights()` in `src/ump/api/processes.py` and the equivalent check
in `Process.__init__` (`src/ump/api/models/process.py`).

**Process execution lifecycle** (`src/ump/api/models/process.py::Process`):
1. `Process(process_id_with_prefix)` parses the `"provider:process_id"` identifier,
   checks the process is configured/not excluded, enforces auth, then synchronously
   fetches process metadata from the remote server (`asyncio.run` inside `__init__`).
2. `execute()` optionally short-circuits via `check_for_cache()` (hash of
   params+version+user, only for `deterministic` processes), otherwise POSTs the job
   to the remote server with `Prefer: respond-async`, creates a local `Job` row
   (status `accepted`), and returns immediately (HTTP 201) with `{jobID, status}`.
3. A **background thread** (`multiprocessing.dummy.Process`, i.e. a real OS thread,
   started from `execute()`) polls the remote job status on an interval
   (`UMP_REMOTE_JOB_STATUS_REQUEST_INTERVAL`) until it reaches a terminal state
   (`successful`/`failed`/`dismissed`), updating the local `jobs` row after every poll.
   This means job progress is only observed by whichever process happened to handle
   the original execution request — there's no separate worker/queue.
4. On success, if the process's `result-storage` is `geoserver`, results are fetched
   from the remote server and persisted into a dedicated GeoServer Postgres DB
   (`src/ump/geoserver/geoserver.py`); otherwise clients fetch results directly via
   `jobs/{id}/results`, which proxies to the remote server (`Job.results()`).

**Two separate Postgres databases / SQLAlchemy engines**: the UMP database (jobs,
ensembles, users — `db_engine` in `src/ump/api/db_handler.py`) and a separate
PostGIS database dedicated to GeoServer results (`geoserver_engine`). Don't conflate
the two when adding queries.

**Data access is inconsistent by design-debt, not convention**: `DBHandler` in
`src/ump/api/db_handler.py` wraps a raw `psycopg2` connection pool with hand-built SQL
(used by `Job`, `jobs.py` listing/counting), while `SQLAlchemy` engines/sessions are
used elsewhere (ensembles, geoserver, job hash cache lookups). There's no ORM model
for the `jobs` table — `Job` (`src/ump/api/models/job.py`) manages its own raw
SQL and serialization (`_to_dict`/`_init_from_dict`). Expect to touch raw SQL when
changing job fields, and update the corresponding Alembic migration under
`migrations/versions/`.

**Providers registry** (`src/ump/api/providers.py`) is a module-level, lock-guarded
dict (`PROVIDERS`) rather than something reloaded per-request — read it via the
`get_providers()`/`get_provider()`/`get_process_config()` accessors, don't mutate it
directly, and don't assume changes to `providers.yaml` require a process restart.

**Error handling**: OGC API Processes-compliant errors go through
`OGCProcessException`/`OGCExceptionResponse` (`src/ump/api/models/ogc_exception.py`,
`src/ump/errors.py`) and are rendered as `application/problem+json` by the
`app.errorhandler(OGCProcessException)` in `main.py`. Prefer raising this type (with
an appropriate `type`/`title`/`status`/`detail`/`instance`) over generic exceptions
when surfacing errors across the API boundary.

## Repo layout

- `src/ump/` — application package (see architecture above for the request/job flow)
- `migrations/` — Alembic migrations for the UMP database (`flask db ...` / `alembic ...`)
- `docs/` — Jupyter Book documentation source, published to
  https://citysciencelab.github.io/urban-model-platform/
- `charts/` — Helm chart for k8s deployment (own version, bumped independently of the app)
- `modelserver_example/` — git submodule; a pygeoapi-based example OGC API Processes
  server used as a reference/test model server in the dev stack
- `docker-compose-dev.yaml` / `docker-compose-prod.yaml` / `docker-compose-build.yaml` —
  dev stack, production reference deployment, and image build, respectively
