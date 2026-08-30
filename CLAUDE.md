# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Urban Model Platform (UMP) — an OGC API Processes gateway. It does not execute models itself: it federates
several remote OGC API Processes servers ("providers", configured in `providers.yaml`), exposes their
processes under a namespaced id `<provider-name>:<process-id>`, forwards execute requests, and owns the
local job lifecycle (status polling, status derivation, result proxying).

This branch (`v3.0.0alpha/base`) is a **full rewrite**: FastAPI + hexagonal architecture in `src/ump/`.
The v2 Flask application still sits in `old_src/` for reference only — do not extend it. Parts of the
docs (`docs/content/`), `CONTRIBUTING.md`, and the `Makefile` still describe the Flask app and stale
compose service names; trust the code over those.

## Working conventions (from `reports/REF-*.md`)

Every file in `reports/` repeats a "Notes for the assistant" header. Honour it:

- **Explicit dependency injection.** Never instantiate an adapter inside another adapter — construct it
  in the composition root and inject it. (The notes say `main.py`; the root has since moved to `asgi.py`.)
- **Keep the core free of framework code.** No FastAPI/SQLModel/aiohttp imports under `core/`.
- **Include small tests with proposed changes**, and run quick syntax/type checks.
- `providers.yaml` uses the **list-based** format under a `providers:` key, not the old dict-keyed format.
- For **"ensembles"**: ask for reference code to gain insight, but do not reuse it — find a better
  solution and say so explicitly.

## `reports/` — the refactor's working spec

`reports/` is the design-and-status record for the rewrite and the best starting point for any
non-trivial change. `REF-00-overview.md` is the index plus a component-by-component status table;
`REF-Refactoring-status.md` is the long-form log; `REF-F0`…`REF-F9` are per-feature specs.

Feature status per those docs: F0 landing page, F1 API versioning, F2 processes, F3 jobs/execution
proxy/persistence, F4 JWT auth, F6 execution pipeline, F7 remote auth ✅ implemented; F8 execution proxy
partially (basic binary-safe proxy done, output-format negotiation and large-input handling specced but
not built); **F5 result storage (geoserver / ldproxy adapters) and F9 horizontal scaling are not started**
— note that `providers.yaml` already advertises `result-storage: ldproxy`, so that key is currently
config-only. `REF-IDEAS.md` holds unscheduled ideas.

These docs drift from the code — verify paths before trusting them. Known stale references: the job repo
is `adapters/job_repository_sql.py` (docs say `sqlmodel_job_repository.py`) and the composition root is
`asgi.py` (docs say `main.py`). `QUALITY_ISSUES.md` is likewise a pre-rewrite audit of the v2 code and
points at `src/ump/api/*` files that no longer exist — it motivated this rewrite rather than describing it.

## Commands

Dependencies are managed by Poetry against a conda env at `./.venv` (`poetry env info --path` confirms).
The env is often stale after a branch switch — run `poetry install` before anything else.

```bash
poetry install                      # deps + editable install of `ump`
poetry run pytest                   # full suite
poetry run pytest tests/test_job_manager_feature_iii.py::test_name   # single test
poetry run black src tests && poetry run isort src tests   # line-length 88, isort profile=black
poetry run flake8 src tests         # ruff is configured in pyproject.toml but is NOT a dependency

poetry run ump                      # dev server → uvicorn on ump.asgi:app
poetry run ump-migrate upgrade head # alembic wrapper reading UMP_DATABASE_* env
poetry run alembic upgrade head     # equivalent, alembic.ini → ./migrations
```

Local dev without any infrastructure: `UMP_JOB_STORE=memory` (the default) plus the bundled mock
provider — `PYTHONPATH=src poetry run uvicorn scripts.mock_ogc_server:app --port 5001` — which serves
`echo`, `hello-world`, `slow` and `failing-job` and is already wired into `providers.yaml.example`.
Backing services (postgres, keycloak, geoserver, mock server) are in `docker-compose-dev.yaml`.

Versioning is driven by `bump-my-version` (`.bumpversion.toml`, app; `charts/`, chart) via the
`bump-app-version` / `bump-chart-version` Make targets — do not hand-edit versions.

## Architecture

Hexagonal / ports-and-adapters. The dependency rule is enforced by convention and matters:

- `src/ump/core/interfaces/` — ports (Protocols): `ProvidersPort`, `HttpClientPort`, `JobRepositoryPort`,
  `AuthPort`, `RemoteAuthPort`, `PollLockPort`, `RetryPort`, `LoggingPort`, `SiteInfoPort`, …
- `src/ump/core/` — managers, models, services. **Must not import from `adapters/`.** Even logging goes
  through `core/settings.py`'s delegating logger, which the composition root swaps for a real adapter.
- `src/ump/adapters/` — concrete implementations (aiohttp, JWT/JWKS, SQLModel job repo, in-memory job
  repo, Postgres advisory poll lock, tenacity retry, providers-file watcher, FastAPI web adapter).
- `src/ump/asgi.py` — **the single composition root.** All wiring happens here, at module import, so each
  uvicorn/gunicorn worker builds its own adapter instances. `main.py`/`cli.py` only launch uvicorn against
  it. Add a new adapter by writing the port impl and wiring it here, nowhere else.

`src/ump/api/` and `src/ump/geoserver/` are empty leftovers.

### Job execution

`JobManager` (`core/managers/job_manager.py`) runs execution as an explicit **pipeline** of
`PipelineStep`s defined in `core/managers/steps/execution_steps.py`, in order: validate/resolve →
create local job → persist accepted → forward to provider → handle provider response → derive statusInfo
→ finalize → shape client response → initiate polling. Each step mutates a shared `JobExecutionContext`
and can set `should_halt`. New lifecycle behaviour belongs in a step, not inline in `JobManager`.

Providers answer execute requests inconsistently, so status is resolved by a **strategy chain**
(`status_derivation_orchestrator.py` → `status_derivation_strategies.py`), first match wins:
direct statusInfo body → immediate results (sync execution) → Location-header follow-up → fallback failed.

State changes fan out to **observers** (`core/managers/observers.py`): status-history persistence,
polling scheduling, and results verification (a provider claiming `successful` is downgraded to `failed`
if its `/results` cannot actually be fetched). Observers are attached in `asgi.py`.

Jobs carry a local UUID; remote self/results links are rewritten to local ones
(`core/utils/link_rewriter.py`, gated by `UMP_REWRITE_REMOTE_LINKS`).

### Configuration and auth

- `core/settings.py` — one `UmpSettings` pydantic-settings object, every knob prefixed `UMP_`, read from
  env and `.env` (see `.env.example`). Read config through `app_settings`, never `os.environ` directly.
- `providers.yaml` (from `providers.yaml.example`) — per-provider url, outbound auth
  (NoAuth/BasicAuth/ApiKey/BearerToken), and the explicit allowlist of exposed processes with
  per-process `anonymous-access`, `ttw-job-done`, `poll-interval`, `result-storage`, `deterministic`.
  Only listed processes are exposed. The file is hot-reloaded by a watcher in `ProviderConfigFileAdapter`.
- Inbound auth is JWT/JWKS (`JwtAuthAdapter`), off by default (`UMP_AUTH_ENABLED=false`).
  Authorization is role-based and lives in `core/services/authorization.py`: role `{provider}` grants the
  whole provider, `{provider}:{process}` a single process, and `anonymous-access: true` bypasses auth.
- Job store is switchable: `UMP_JOB_STORE=memory` (default, file-backed scratch dir, dev/TDD only) or
  `postgres` (SQLModel + asyncpg + Postgres advisory locks so multiple workers do not double-poll a job).

### Tests

`tests/` has no conftest; tests import `ump` from the installed package and use hand-written in-test
adapters implementing the ports rather than mocks of concrete classes. Follow that pattern — it is the
main payoff of the port layer. `pytest-randomly` is installed, so tests must not depend on ordering.
