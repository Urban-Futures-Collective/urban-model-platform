_Last_updated: 2026-09-26

# Notes for the assistant

- The user prefers explicit dependency injection. Do not instantiate adapters inside adapters; instantiate them in `main.py` and inject.
- Keep the core free of framework code.
- When proposing changes, include small tests where feasible and run quick syntax/type checks.
- `providers.yaml` uses a list-based format under a `providers:` key — not the old dict-keyed format. See `providers.yaml.example`.
- When the user asks for implementation details for "ensembles": ask for reference code to gain insights; do not reuse the provided code — find a better solution and inform the user.

# Feature XII: Model registry 🔲 (not started)

A registry of the models offered through UMP: the model card, the review/verification
workflow, the provider servers and processes that today live in `providers.yaml`, and an
audit log. It turns "a model" from an entry in a hand-edited YAML file into a managed object
with an owner, a lifecycle and a documented interface.

Inputs to this design:
- the UMP-X model-submission notes (model card fields, test run, server, audit log);
- the platform roles and permissions matrix (provider, verifier, access-admin,
  platform-admin, user, viewer);
- `REF-F11-role-administration.md` — Keycloak is the single source of truth for **all**
  roles, administered from UMP-X.

Artefacts:
- UML: [`docs/diagrams/model-registry-uml.mmd`](../docs/diagrams/model-registry-uml.mmd)
  (rendered `.svg` / `.png` next to it);
- reference DDL: [`REF-F12-registry-schema.sql`](REF-F12-registry-schema.sql).

## Decisions recorded

| # | Decision | Consequence |
|---|---|---|
| D1 | The registry lives in the **existing UMP database** (`ump-db`), in its own schema `registry`, managed by the existing Alembic migrations. | No new service; jobs and models can be joined; one backup. Not in Keycloak's database — that schema is private to Keycloak. |
| D2 | **All roles live in Keycloak** (F11): the six platform roles and the model-access roles `{provider}` / `{provider}:{process}`. | The registry stores **no grants**. Whether a user may run a model is decided from the token alone, exactly as today. |
| D3 | **No visibility level** (public / municipal / private) for now. | Execution access = model-access role or `anonymous-access` (plus `verifier`, D7). Catalogue listing stays governed by `UMP_PUBLIC_PROCESSES` (D10). |
| D4 | **No comments or ratings** for now. | Can be added later as separate tables without touching the rest. |
| D5 | **Interface drift policy A — flag only.** When a provider's server changes a process interface, UMP records and shows it but keeps executing. | No outages caused by drift; the "verified" status can silently go stale — mitigated by making the drift visible (see [Interface drift](#interface-drift-policy-a)). |
| D6 | **One owner per model** (`model.owner_sub`), not a Keycloak role. | "Edit own model" = `provider` role from the token + `owner_sub = caller`. A `platform-admin` can transfer ownership. |
| D7 | **Verifiers can run models** under review and published ones, without holding the model-access role. | `ProcessConfig.state` decides; no registry lookup on the execution path. See [Execution rules](#execution-rules). |
| D8 | **Publishing creates the model-access roles in Keycloak automatically.** | Done by UMP-X, not UMP (UMP keeps no Keycloak write path, F11). See [Publishing and Keycloak roles](#publishing-and-keycloak-roles). |
| D9 | **Deactivating a model stops new runs immediately.** Running jobs finish; their status and results stay retrievable. | Deactivated models stay in the provider snapshot with `state = deactivated`; execution is refused. Requires prerequisite [P1](#prerequisite-p1-reject-unconfigured-processes). |
| D10 | **The registry catalogue follows `UMP_PUBLIC_PROCESSES`.** | `true`: published model cards are readable by anyone, including anonymous callers. `false`: `viewer` (or `user`) required. |
| D11 | **Retention: 3 months** for the audit log. | Daily delete by `ump-retention`. See [Retention](#retention-3-months). |
| D12 | **Minimal schema: four tables.** One row per model; descriptive model-card parts as JSONB; workflow rules in the core. | See [Simplification](#simplification-what-was-dropped-and-why). |
| D13 | **Multilingual model cards** via language maps per field (`{"de": "…", "en": "…"}`), with a per-model default language that is always required. | No extra table; read API resolves one language with fallback. See [Multilingual content](#multilingual-content). |

## Simplification: what was dropped and why

An earlier draft had 19 tables. Most of them normalised model-card content that is only
ever displayed, never joined or filtered. The current schema keeps a table only where there
is a relation UMP actually queries (server → models, user → models) or a lifecycle of its
own (audit log).

| Earlier | Now | What is lost |
|---|---|---|
| `app_user` + `person` + `model_contributor` | `app_user` (accounts only, PK = JWT `sub`) + `model.contributors` JSONB | People without an account are plain entries on a model card, not reusable records; no ORCID uniqueness across models. |
| `organization` | free text inside `contributors` / `applications` | No structured institution list. It existed for the "municipal" visibility level, which was dropped (D3). |
| `license` lookup | `model.license` text (SPDX expression, validated in the core) | No FK to a licence list. |
| `version_status` + `version_status_transition` + triggers | `status` CHECK + transition graph in `VerificationService` | No database-level transition check. The core enforces it, and it already had to (roles are only visible there). |
| `model` + `model_version` (versioning) | one `model` row with `status` and `version_label` | **No parallel "published v1 + draft v2"**, no stored history of model-card versions. See the note below. |
| `model_review` | `verified_by`, `verified_at`, `verification_job_id`, `status_note` on `model`; history in the audit log | Review history older than 3 months (audit retention). The current verification is always kept. |
| `model_maintainer` | `model.owner_sub` | Co-maintainers. Ownership is transferable by `platform-admin`. |
| `model_version_io` | the OGC process description (`verified_interface`) + `io_notes` JSONB | Inputs/outputs are no longer rows; they are read from the description, which is the authority anyway. |
| `model_test_case`, `model_reference`, `model_application` | `test_cases`, `literature`, `applications` JSONB | Not queryable as rows (a GIN index can be added if search is needed). |
| `model_deployment` | columns on `model` + `process_config` JSONB | One model = one remote process (was already 1:1 in practice). |
| `process_interface_snapshot` (history) | `verified_interface` + `current_interface_sha256` on `model` | No history of past interfaces; drift events with a diff summary are in the audit log for 3 months. |
| `credentials_secret_ref` alternative | `credentials` (encrypted) only | Ops can no longer point at a Kubernetes secret; the import command encrypts existing credentials instead. |
| monthly partitions + retention functions | plain table + daily `DELETE` | Nothing relevant at the expected volume. |
| `deprecated` status, `hosting`, server operator | — | Can be a note on the model card. |

**Versioning — the one real trade-off.** Without `model_version`, a published model is
edited in place:
- **Model-card edits** (texts, contributors, literature, requirements) by the owner do not
  change the status and need no new review. They are logged in the audit log.
- **The verified interface** stays pinned (`verified_interface`, `verified_interface_sha256`).
  Re-verifying after drift updates these columns in place; the model stays `published`.
- **A reset** (`published → in_review`) blocks `user` runs until it is published again,
  because the same row cannot be published and under review at once.

If card edits of published models must be reviewed before they become visible, or users
must keep running v1 while v2 is reviewed, `model_version` has to come back (+1 table,
+ the "currently served version" pointer). Nothing else in the schema depends on this
choice.

## Scope

**In scope (UMP):**
- registry schema + Alembic migration;
- provider onboarding: register a model server, discover its processes, pre-fill model cards;
- model card editing;
- review workflow (submit → review → verify → publish; reset; four-eyes rule);
- drift detection (policy A);
- a database-backed `ProvidersPort` adapter that replaces `providers.yaml` as the source of
  provider/process configuration;
- platform-role checks for the new endpoints;
- audit log for registry actions and job executions.

**Out of scope:**
- role and account administration → F11 (UMP-X, Keycloak Admin API);
- comments, ratings, visibility levels (D3, D4);
- billing/metering (see `REF-IDEAS.md`, "Usage accounting");
- detecting *behavioural* changes of a remote model (same interface, different results).

## Architectural position

```
 provider / verifier / platform-admin
        │  (UMP-X, user's token)
        ▼
 ┌──────────────────────────── UMP ─────────────────────────────┐
 │ adapters/web: /v1.0/registry/*  (new router)                 │
 │        │                                                     │
 │ core/services: ModelSubmissionService, VerificationService,  │
 │                InterfaceDriftService, (AuthorizationService) │
 │        │ ports                                               │
 │ ModelRegistryPort ── AuditLogPort ── CredentialCipherPort    │
 │ (+ existing HttpClientPort, RemoteAuthPort, PollLockPort)    │
 │        │                                                     │
 │ ProvidersPort ◄── ProviderConfigDbAdapter (reads registry)   │
 │        │            replaces ProviderConfigFileAdapter       │
 │ ProcessManager / JobManager / AuthorizationService unchanged │
 └──────────────────────────────────────────────────────────────┘
        │ SQL (schema registry)           │ HTTP (discovery, drift checks)
        ▼                                 ▼
     ump-db                        provider OGC API Processes servers
```

UMP still has **no write path to Keycloak** (F11). Roles arrive in the token; the registry
only reads them.

## Prerequisite P1: reject unconfigured processes

Today the execution path does **not** check that a process is configured:

- `ValidateAndResolveStep` (`core/managers/steps/execution_steps.py`) only resolves the
  provider; `_find_remote_id` falls back to the bare remote id when the process is not
  listed.
- `ProvidersPort.check_process_availability` (which honours `exclude`) is never called.
- `AuthorizationService` grants `{provider}` access to *every* process of that provider,
  listed or not.

Consequence: a holder of the `{provider}` role can execute any process the remote server
exposes, including processes marked `exclude: true` or not configured at all. With the
registry this would also bypass deactivation (D9) and review-only access (D7).

Fix (independent of the registry, can ship first): `ValidateAndResolveStep` halts with
404 when `get_process_config(provider, id)` finds no configured, non-excluded process, and
the bare-id fallback is removed for configured providers. Add a regression test for an
excluded and an unlisted process.

## Data model

Schema `registry`, four tables. Full DDL in `REF-F12-registry-schema.sql`; diagram in
`docs/diagrams/model-registry-uml.mmd`. Enumerations are `text` + `CHECK` (not PG `ENUM`)
so Alembic can evolve them. The only trigger guards the audit log.

| Table | Purpose |
|---|---|
| `app_user` | Accounts seen by UMP. PK `sub` = JWT `sub` = `public.jobs.user_id`. `display_name`, `email` are a cache of token claims. **No roles, no password.** |
| `provider_server` | One OGC API Processes server = one `providers:` entry. `name` (process-id and role prefix, immutable), `base_url`, `owner_sub`, `auth_type`, encrypted `credentials`, `ttw_job_done_seconds`, `is_active`. |
| `model` | One remote process: identity (`slug`, `provider_server_id` + `remote_process_id`, `owner_sub`), model card (multilingual, `default_lang`), `process_config` (the providers.yaml process fields), lifecycle (`status`, generated `exec_state`), verification, drift state. |
| `audit_log` | Append-only. Actor `sub`, **role snapshot from the token**, action, `model_id`, `job_id`, outcome, details. 3-month retention. |

Plus one view, `v_providers_config`, the source for `ProviderConfigDbAdapter`.

JSONB columns on `model` are validated by Pydantic models in the core
(`core/models/registry/`), not by the database; the database only checks their JSON type
(array / object). `L` denotes a language map (see [Multilingual content](#multilingual-content)):

| Column | Shape |
|---|---|
| `full_name`, `short_description`, `purpose`, `limitations_risks` | `L` |
| `keywords` | `{"<lang>": ["…", …]}` |
| `requirements` | `{cpu_cores, memory_gb, gpu, storage_gb, runtime_seconds, software}` |
| `contributors` | `[{name, role: author|developer|contributor|maintainer|funder, orcid?, affiliation?, email?}]` |
| `literature` | `[{citation, doi?, url?, year?}]` |
| `applications` | `[{title: L, description?: L, location?, url?, year?}]` |
| `test_cases` | `[{name, description?: L, input_url, expected_output_url, documentation_url?, input_sha256?, output_sha256?}]` |
| `io_notes` | `{"<ogc input/output id>": L}` — format notes (CRS, units, …) |
| `process_config` | the process entry of `providers.yaml`: `anonymous-access`, `result-storage`, `result-path`, `graph-properties`, `ttw-job-done`, `poll-interval`, `deterministic` |

Model-card field mapping (from the submission notes):

| Model card field | Stored in |
|---|---|
| Full name / slug | `model.full_name` / `model.slug` |
| Code availability | `model.repository_url` + `model.code_ref` |
| License | `model.license` (SPDX) |
| Purpose and applications | `model.purpose` + `model.applications` |
| Input / output (format) | the OGC process description (`verified_interface`) + `model.io_notes` |
| Hardware/software specs, compute estimate | `model.requirements` |
| Authors, developers, contributors | `model.contributors` |
| Existing applications | `model.applications` |
| References / literature | `model.literature` |
| Limitations & risks | `model.limitations_risks` |
| Contact | the owner (`model.owner_sub` → `app_user.email`) |
| Documented test run | `model.test_cases` |
| Server URL | `provider_server.base_url` |

Database invariants (CHECK constraints): `full_name` always contains `default_lang`; a
model enters review only with `purpose` in `default_lang` and a `license`; `verified`/`published` models always carry `verified_by` and
`verified_interface_sha256`; `verified_by ≠ owner_sub` (four-eyes); credentials present iff
`auth_type ≠ NoAuth`; `(provider_server_id, remote_process_id)` unique.

`public.jobs` is **not** changed. It joins to the registry via
`provider_server.name || ':' || model.remote_process_id = jobs.process_id` and
`jobs.user_id = app_user.sub`. The audit log references jobs and models by id without a
foreign key, because it must outlive both.

## Multilingual content

Providers may describe a model in several languages (D13).

**Storage.** Every free-text model-card field is a language map `L`:
`{"de": "Wachstum des Radnetzes", "en": "Bike network growth"}`. This applies to
`full_name`, `short_description`, `purpose`, `limitations_risks`, `keywords` (a list per
language), `io_notes` values and the text fields inside `applications` and `test_cases`.
Not translated: slug, licence, URLs, `code_ref`, `version_label`, contributor names,
`process_config`.

**Rules.**
- `model.default_lang` (default `de`) is the language that is always complete. The
  database enforces it for `full_name` from creation and for `purpose` before review; the
  core enforces it for all other `L` fields that are filled at all.
- Allowed language codes come from `UMP_REGISTRY_LANGUAGES` (default `de,en`, matching
  UMP-X's i18n). The core rejects other keys and empty strings; the database only checks
  the `default_lang` format.
- One core type, `LocalizedText = dict[LangCode, NonEmptyStr]`, with a
  `resolve(lang, default_lang)` helper, is used for every `L` field.

**API.**
- Read endpoints resolve one language: `?lang=` or `Accept-Language`, falling back to
  `default_lang`. The response carries `Content-Language` and, per model,
  `lang` (delivered) and `languages` (available), so UMP-X can mark fallbacks.
- `?lang=*` returns the full maps (needed by the edit form).
- Write endpoints accept full maps; a PATCH replaces a field's whole map.
- `providers.yaml` / `ProcessConfig.description` and the MCP catalog use the
  default-language `full_name` until they get their own language handling.

**Onboarding.** Discovery requests `GET /processes/{id}` once per configured language with
`Accept-Language`. Titles and descriptions that differ from the default-language response
are stored as that language's entry; servers that ignore `Accept-Language` simply yield the
default language only. The interface hash is computed from the default-language response
(titles and descriptions are excluded from it anyway).

**Search.** `keywords` has a GIN index (`jsonb_path_ops`, e.g.
`keywords @> '{"en": ["cycling"]}'`). Full-text search, if needed, gets one expression index
per platform language (see the DDL comment).

**Known limits.**
- Translations can go stale: editing the German `purpose` does not mark the English one as
  outdated. The audit log records which language changed when, but nothing flags it in the
  UI (see open questions).
- Translations are model-card content, so under D12 they are not reviewed; a wrong
  translation goes live immediately.

## Review workflow

```
draft → submitted → in_review → verified → published
            ↑           │ │                    │
            └── changes_requested   rejected    └→ in_review (reset)
any of in_review / verified / published → deactivated
```

| Transition | Performed by (platform role) |
|---|---|
| `draft → submitted`, `submitted → draft`, `changes_requested → submitted` | `provider`, owner of the model |
| `submitted → in_review`, `in_review → verified / changes_requested / rejected` | `verifier`, not the owner |
| `verified → published`, `published → in_review` (reset) | `verifier`, not the owner |
| `in_review / verified / published → deactivated` | `verifier` or `platform-admin` |

Rules:
- The transition graph is **domain logic in the core** (`VerificationService`). The
  database only holds the invariants listed under [Data model](#data-model).
- **Four-eyes:** the owner never verifies their own model, even when holding `verifier`
  (CHECK `verified_by ≠ owner_sub`).
- **Verify** (`in_review → verified`) stores the current raw process description as
  `verified_interface` + its hash, the verifier's `sub`, the time and the verification job.
- **Card edits** by the owner are allowed in every status except `deactivated` and do not
  change the status (see the versioning note in [Simplification](#simplification-what-was-dropped-and-why)).
- **Re-verification** of a published model (e.g. after drift) updates the `verified_*`
  columns in place; the model stays `published`.
- `status_note` holds the last reviewer comment or rejection/deactivation reason;
  `status_changed_by/at` the last change. The full history is in the audit log.
- **Publishing and model access:** publishing creates the model-access roles (D8) but does
  **not** assign them to anyone. `published_at` is set on the first publication only.

## Execution rules

`ProviderConfigDbAdapter` puts every model with a non-NULL `exec_state` into the provider
snapshot. `exec_state` is a generated column; `ProcessConfig` (core model) gets it as a
new field:

```python
state: Literal["published", "in_review", "deactivated"] = "published"
```

The default keeps `ProviderConfigFileAdapter` and every existing `providers.yaml`
unchanged.

| `status` | `exec_state` | Who may start a run |
|---|---|---|
| `draft`, `submitted` | NULL — not in the snapshot | — |
| `in_review`, `verified` | `in_review` | `verifier` only; `anonymous-access` and model-access roles do not apply |
| `published` | `published` | as today (`anonymous-access`, `{provider}`, `{provider}:{process}`) **or** `verifier` |
| `changes_requested`, `rejected`, `deactivated` | `deactivated` | nobody — 403 `Model deactivated`, checked **before** the anonymous-access shortcut |

`changes_requested` and `rejected` map to `deactivated` rather than dropping out of the
snapshot: a verifier may have started a run while the model was in review, and running jobs
need the provider entry for polling (`JobManager`, `get_provider(job.provider)`) and result
proxying.

Listing (`GET /v1.0/processes`, MCP catalog): `deactivated` is never listed; `in_review` is
listed only for callers holding `verifier`, independent of `UMP_PUBLIC_PROCESSES`.

Verification runs are ordinary jobs. The verifier links the job in
`model.verification_job_id`; the audit log records the run with the role snapshot showing
`verifier`.

## Publishing and Keycloak roles

UMP has no write path to Keycloak (F11), so role creation happens in UMP-X:

1. The verifier publishes in UMP-X. The request goes through a Nitro route
   (`server/api/registry/models/[id]/publish.post.ts`) that forwards to
   `POST /v1.0/registry/models/{id}/transitions` with the **verifier's own token**.
2. On success, the same route creates the realm roles `{provider}` (if missing) and
   `{provider}:{process}` via the service account. "Already exists" (409) counts as success.
3. A **reconciler** (`server/api/admin/access/sync-roles.post.ts`, also run when the access
   admin page loads) compares `GET /v1.0/registry/roles/vocabulary` with Keycloak's realm
   roles and creates missing ones. This repairs any publish where step 2 failed after
   step 1 succeeded.

The vocabulary is every model with `published_at IS NOT NULL`. Roles are **never deleted**
automatically — deactivation is enforced by UMP (D9); deleting the role would erase the
record of who had access. The service account needs `manage-realm` for role creation (see
F11).

## Provider onboarding and interface discovery

1. A `provider` registers a model server: `base_url`, auth type, credentials.
2. UMP validates the URL ([Outbound request safety](#outbound-request-safety)) and calls
   `GET {base_url}/processes`, then `GET {base_url}/processes/{id}` for each process the
   provider selects.
3. Per selected process UMP creates one `model` row (`draft`, owner = caller), pre-filled:

   | From the OGC process description | Into |
   |---|---|
   | `title` | `full_name[default_lang]` (+ other languages, see [Multilingual content](#multilingual-content)) |
   | `description` | `short_description[default_lang]` (+ other languages) |
   | `keywords` | `keywords[default_lang]` |
   | `version` | `version_label` (editable) |
   | full body | shown to the verifier; stored as `verified_interface` on verification |
   | interface hash | `current_interface_sha256` |

4. The provider completes what OGC does not describe: licence, purpose, limitations,
   contributors, literature, applications, requirements, test cases, I/O format notes —
   at least in the default language — then submits.

**Use the raw remote description.** `ProcessManager._fetch_process` runs the description
through handlers (`_handle_process_id`, `_handle_fill_defaults`,
`_handle_sanitize_metadata`, `_handle_rewrite_links`). Discovery must hash the body
*before* those handlers; otherwise a change to UMP's own handlers would look like provider
drift. Discovery therefore uses `HttpClientPort` + `RemoteAuthPort` directly, not
`ProcessManager.get_process`.

**Interface hash.** SHA-256 over canonical JSON (sorted keys, no whitespace) of
`{inputs, outputs, jobControlOptions, outputTransmission}` with `title`, `description`,
`links` and `metadata` removed at every level. Text edits on the provider side do not
change the hash; schema, cardinality and execution-mode changes do. The OGC `version` field
is **not** used for detection — providers do not reliably bump it.

## Interface drift (policy A)

The provider controls the server, so UMP can only **detect** interface changes, not prevent
them. "Verified" therefore means *verified against `verified_interface` at `verified_at`*.

**Check.** `InterfaceDriftService.check_all()` runs periodically
(`UMP_INTERFACE_CHECK_INTERVAL_SECONDS`, default 3600) and on demand ("I updated my server"
button, `POST /v1.0/registry/servers/{id}/rediscover`). Per model with a non-NULL
`exec_state`:

| Outcome | `interface_state` | Side effects |
|---|---|---|
| hash equals `verified_interface_sha256` (or no verification yet and hash unchanged) | `in_sync` | `current_interface_sha256`, `last_checked_at` updated |
| hash differs from `verified_interface_sha256` | `drifted` | audit event `model.interface.drifted` with a diff summary (on state change only) |
| server unreachable / non-2xx / invalid JSON | `unreachable` | audit event on state change only |
| drifted model returns to the verified hash | `in_sync` | audit event `model.interface.restored` |

**Policy A — flag only:**
- Execution is **not** blocked. No pipeline step changes.
- `interface_state` and a diff (live description vs `verified_interface`, computed on
  request) are exposed on the registry API so UMP-X can show a warning on the model page
  and in the verifier's queue.
- Resolution: a verifier re-verifies in place (new `verified_interface`), or resets the
  model to `in_review` if the change needs a real review.

**Known weakness of A.** A drifted model keeps its `published` badge while its interface no
longer matches what was verified. The UI warning is the only mitigation. Moving to a
stricter policy later needs no schema change:
- *Policy B* (block until re-verified) = one new `PipelineStep` after
  `ValidateAndResolveStep` that rejects processes in `interface_state = drifted`
  (the state then has to be part of the provider snapshot).
- *Policy C* (block only breaking changes) = B plus a classifier in `InterfaceDriftService`
  (removed input, new required input, changed type → breaking; added optional input → not).

**Multiple workers.** Only one worker may run the check. Reuse `PollLockPort` with a fixed
key. `PgAdvisoryPollLock` converts the key via `UUID(job_id)`, so the key must be a UUID
string — use a constant such as `uuid5(NAMESPACE_URL, "ump:registry:interface-check")`.

## Provider credentials

Providers enter their server login in UMP-X, so UMP now stores secrets that previously only
ops handled (in `providers.yaml` / Kubernetes secrets).

- `provider_server.credentials` holds the encrypted secret; present iff `auth_type ≠ NoAuth`.
- Encryption through a new `CredentialCipherPort`; adapter uses authenticated encryption
  (e.g. Fernet / AES-GCM) with the key from `UMP_REGISTRY_CREDENTIALS_KEY`. A key-id prefix
  in the ciphertext allows rotation.
- **Write-only API.** Credentials are never returned; responses show only `auth_type` and
  `credentials_set: true|false`.
- `ProviderConfigDbAdapter` decrypts when building `ProviderConfig.authentication`, which
  already holds `SecretStr`, so `RemoteAuthAdapter` needs no change.
- The import command encrypts the credentials it reads from `providers.yaml`.

## Outbound request safety

Registering a server makes UMP fetch a URL chosen by a user. Without checks this is a
server-side request forgery path into the cluster (internal services, cloud metadata
endpoints).

- Scheme `https` (allow `http` only when `UMP_REGISTRY_ALLOW_HTTP=true`, dev only).
- Resolve the host and reject private, loopback, link-local and metadata ranges unless
  explicitly allowlisted (`UMP_REGISTRY_ALLOWED_PRIVATE_HOSTS`, needed for in-cluster model
  servers such as `modelserver:5000`).
- No redirects to a different host; timeout ≤ 10 s; response size cap (e.g. 2 MB for
  process descriptions).
- The same checks apply to re-discovery and drift checks, since `base_url` can be edited.

## Authorization

Two layers, both evaluated in the core:

1. **Platform role from the token** (`AuthContext.roles`) — which kind of action is allowed.
2. **Ownership** — `owner_sub` of the model or server.

| Action | Platform role | Ownership check |
|---|---|---|
| Register server, create model | `provider` | — |
| Edit model card, submit, withdraw submission | `provider` | caller is owner |
| Pick up / decide review, verify, publish, reset | `verifier` | caller is **not** owner |
| Deactivate model | `verifier` or `platform-admin` | — |
| Transfer ownership, edit any server | `platform-admin` | — |
| Read audit log | `verifier`, `access-admin` or `platform-admin` | — |
| Browse catalogue / model cards | anyone if `UMP_PUBLIC_PROCESSES=true`, else `viewer` (or `user`, composite) — D10 | published models only, unless owner/verifier |
| Execute | see [Execution rules](#execution-rules) | `ProcessConfig.state` (no DB lookup) |

Extend `AuthorizationService` with `require_platform_role(auth, *roles)` and keep role names
in one core constant (`core/models/registry/roles.py`), shared by all registry services.
`UMP_AUTH_ENABLED=false` must not turn the registry write endpoints into open endpoints:
with auth disabled, registry writes are rejected (same trap as described in F10).

## Identity: `app_user`

`app_user` is upserted just-in-time on the first authenticated registry request and
`last_seen_at` refreshed (debounced). This needs more than today's `AuthContext`
(`user_id`, `roles`, `is_authenticated`): `JwtAuthAdapter` must also pass `email` and
`name`. Add them as optional fields to `AuthContext`; existing callers are unaffected.

The PK is `sub` alone, which assumes a single Keycloak realm (issuer). Account state is
authoritative in Keycloak (F11); a disabled user cannot obtain a token, so `app_user` has
no active flag.

## Audit log

- Registry services write through `AuditLogPort` in the same transaction as the change.
- Job executions: a new `AuditObserver` (a `JobStateObserver`) records `job.execute` and
  terminal outcomes with `job_id`; inputs are **not** copied (they are in `public.jobs`).
- Model-card edits record a before/after diff in `details` — this replaces version history.
- Role grants/revokes are **not** visible to UMP; they are audited by Keycloak admin events
  (see F11).

## Retention (3 months)

- `ump-retention` runs daily as a Kubernetes CronJob (Helm chart) and deletes audit records
  older than 3 months:

  ```sql
  BEGIN;
  SET LOCAL ump.audit_retention = 'on';
  DELETE FROM registry.audit_log WHERE occurred_at < now() - interval '3 months';
  COMMIT;
  ```

  The audit guard trigger rejects every UPDATE and every DELETE outside such a
  transaction. This protects against mistakes, not against a malicious app process with the
  same database user; separate database roles would be needed for that.
- The current verification (`verified_*` on `model`) is not affected by retention; only
  older review rounds and drift events disappear with the audit log.
- Note: 3 months also bounds how far back "who ran what" can be answered from the audit
  log. `public.jobs` has no retention yet and keeps `user_id` + inputs indefinitely — out
  of scope here, but it undercuts the 3-month limit for the same data and should get the
  same rule.

## Hexagonal mapping

**Core** (no FastAPI / SQLModel / aiohttp imports):

| Kind | Name | Location | Notes |
|---|---|---|---|
| Models | `AppUser`, `ProviderServer`, `Model` (+ `Requirements`, `Contributor`, `LiteratureRef`, `Application`, `TestCase` for the JSONB parts), `AuditEvent` | `core/models/registry/` | Plain Pydantic, like `core/models/job.py`; they validate the JSONB content |
| Domain logic | status transition graph, `exec_state` mapping, interface hash + canonicalisation, role constants | `core/models/registry/` | Pure functions; unit-tested without adapters |
| Port | `ModelRegistryPort` | `core/interfaces/model_registry.py` | Repository: users, servers, models |
| Port | `AuditLogPort` | `core/interfaces/audit_log.py` | Append-only writer + filtered reader |
| Port | `CredentialCipherPort` | `core/interfaces/credential_cipher.py` | `encrypt(str) -> bytes`, `decrypt(bytes) -> str` |
| Service | `ModelSubmissionService` | `core/services/model_submission.py` | Onboarding, discovery, pre-fill, card edits. Uses `HttpClientPort`, `RemoteAuthPort`, `ModelRegistryPort` |
| Service | `VerificationService` | `core/services/verification.py` | Transitions, four-eyes, verification runs via `JobManager` |
| Service | `InterfaceDriftService` | `core/services/interface_drift.py` | Periodic check, state changes (policy A) |
| Service (extend) | `AuthorizationService` | `core/services/authorization.py` | `require_platform_role`; `state` rules (D7, D9) |
| Observer | `AuditObserver` | `core/managers/observers.py` | Attached in `asgi.py` next to the existing observers |

No new outbound HTTP port: discovery reuses `HttpClientPort` + `RemoteAuthPort`.

**Adapters:**

| Adapter | Implements | Notes |
|---|---|---|
| `registry_repository_sql.py` | `ModelRegistryPort` | SQLModel table models live here only, with `from_domain` / `to_domain` bridges — same two-model pattern as `job_repository_sql.py` |
| `registry_repository_inmemory.py` | `ModelRegistryPort` | For tests and `UMP_JOB_STORE=memory` dev mode |
| `audit_log_sql.py` / `audit_log_inmemory.py` | `AuditLogPort` | |
| `credential_cipher_fernet.py` | `CredentialCipherPort` | Key from settings, injected |
| `provider_config_db_adapter.py` | `ProvidersPort` | See below |
| `adapters/web/registry_router.py` | — (driving) | `/v1.0/registry/*`, included in `fastapi.py` like the MCP router; OGC routes untouched |

**`ProviderConfigDbAdapter` and the synchronous port.** `ProvidersPort` is synchronous
(`get_provider`, `get_process_config`, …) and is called on the hot path by `ProcessManager`
and `AuthorizationService`. The adapter therefore keeps an **in-memory snapshot** of
`list[ProviderConfig]` built from `v_providers_config` and refreshes it asynchronously — on
registry writes in the same worker, and via Postgres `LISTEN/NOTIFY` for other workers
(a poll of ≤ 30 s only as fallback), so that a deactivation reaches all workers quickly
(D9). This mirrors how `ProviderConfigFileAdapter` hot-reloads the file.
`CredentialCipherPort` and the session factory are injected in `asgi.py`; the adapter never
constructs them.

Selection: `UMP_PROVIDERS_SOURCE=file|db` (default `file`). Migration path: a one-off
`ump-import-providers` command reads `providers.yaml` into the registry (one
`provider_server` per provider, one `published` model per listed process, credentials
encrypted, owner = a given `platform-admin` sub), then the setting is switched to `db`.

**Composition root (`asgi.py`):** construct the SQL (or in-memory) registry adapter, the
audit adapter, the cipher, the providers adapter selected by `UMP_PROVIDERS_SOURCE`; inject
them into the new services; attach `AuditObserver`; start the drift-check task in the
lifespan, guarded by `PollLockPort`.

## API sketch (`/v1.0/registry`)

UMP extension, not OGC. JSON only, no trailing slashes (F1 conventions).

| Method | Path | Role |
|---|---|---|
| `POST` | `/servers` | `provider` |
| `GET` | `/servers`, `/servers/{id}` | `provider` (own), `platform-admin` |
| `PATCH` | `/servers/{id}` | owner, `platform-admin` |
| `POST` | `/servers/{id}/discover` · `/servers/{id}/rediscover` | owner, `platform-admin` |
| `POST` | `/servers/{id}/models` — `{remote_process_ids: [...]}` | `provider` |
| `GET` | `/models`, `/models/{id}` — `?lang=` / `Accept-Language`, `?lang=*` for all | anyone / `viewer` per D10 |
| `PATCH` | `/models/{id}` — card fields | owner |
| `POST` | `/models/{id}/transitions` — `{to_status, note?, verification_job_id?}` | per [workflow](#review-workflow); UMP-X wraps `published` to create roles (D8) |
| `POST` | `/models/{id}/reverify` | `verifier`, not owner |
| `PATCH` | `/models/{id}/owner` | `platform-admin` |
| `GET` | `/models/{id}/interface` — state + diff to the verified interface | owner, `verifier` |
| `GET` | `/audit` | `verifier`, `access-admin`, `platform-admin` |
| `GET` | `/roles/vocabulary` — valid model-access role names | `access-admin`, `verifier` (used by F11 and D8) |

## Migrations

`migrations/versions/0003_create_registry_schema.py`, written by hand like `0001`. Reference
DDL: [`REF-F12-registry-schema.sql`](REF-F12-registry-schema.sql) — reflects D1–D12 and was
tested on PostgreSQL 16 (4 tables, credential rule, generated `exec_state`, four-eyes and
card-completeness checks, default-language checks and fallback, keyword search per
language, JSONB type checks, provider view, audit guard and retention delete). The migration can execute it largely verbatim via `op.execute`.

## Implementation phases

0. **Prerequisite P1.** Reject unconfigured/excluded processes on execution. Ships
   independently.
1. **Schema + read side.** Migration 0003, domain models, `ModelRegistryPort` (SQL +
   in-memory), `GET /models`. Import command for `providers.yaml`.
2. **Providers from the database.** `ProviderConfigDbAdapter`, `UMP_PROVIDERS_SOURCE`.
   Existing OGC behaviour must be unchanged — the existing test suite is the acceptance test.
3. **Onboarding + model cards.** Servers, discovery, pre-fill, credentials, outbound checks.
4. **Workflow.** Transitions, verification, platform-role checks, audit log.
5. **Drift (policy A).** Periodic check, state exposure, `AuditObserver`.
6. **Retention.** `ump-retention`, CronJob in the Helm chart.

## Tests

Following the repo convention (hand-written in-test adapters, no mocks of concrete classes,
no ordering dependence):

- transition graph: every allowed transition passes, every other pair is rejected; role
  and ownership rules per transition;
- four-eyes: owner holding `verifier` cannot verify own model;
- JSONB models: invalid contributor role, missing `citation`, malformed `requirements` are
  rejected by the core before reaching the database;
- multilingual: unknown language key and empty string rejected; `L` field without
  `default_lang` rejected; `resolve()` falls back to `default_lang`; read API returns
  `Content-Language` and `languages`; `?lang=*` returns full maps; discovery stores a
  second language only when the server answers `Accept-Language` differently;
- interface hash: title/description/links changes → same hash; added input, changed
  `minOccurs`, changed schema type → different hash; key order irrelevant;
- drift service with an in-test `HttpClientPort`: in_sync → drifted → restored;
  unreachable; audit event only on state change;
- `ProviderConfigDbAdapter` produces `ProviderConfig` equal to what the file adapter produces
  for the same `providers.yaml` (round trip via the import command);
- outbound checks: private/loopback/metadata addresses rejected, allowlisted host accepted;
- credentials: never present in any API response; decrypt(encrypt(x)) == x; wrong key fails;
- authorization matrix for every registry endpoint, including `UMP_AUTH_ENABLED=false`;
- P1: executing an excluded and an unlisted process of a configured provider → 404, also
  for a holder of `{provider}`;
- execution by state: `deactivated` refused for everyone incl. anonymous-access processes;
  `in_review` only for `verifier`; `published` for model-access roles and `verifier`;
- a job started before deactivation still polls to completion and its results remain
  retrievable;
- listing hides `deactivated` for all and `in_review` for non-verifiers;
- `ProcessConfig.state` defaults to `published` — existing file-based config unchanged;
- retention: rows older than 3 months are deleted, newer ones kept; a plain DELETE is
  rejected.

## Open questions

1. **Providers test-running their own model under review.** D7 lets verifiers run models
   under review. Should the owner also be able to (e.g. to reproduce a verifier's finding)?
   That would need the owner relation on the execution path (a registry lookup), unlike D7.
2. **Credential key rotation** procedure (who rotates `UMP_REGISTRY_CREDENTIALS_KEY`, how
   often, re-encryption job).
3. **Versioning (D12).** Confirm that card edits of published models need no review and
   that a reset may block users until republication. Otherwise reintroduce `model_version`.
4. **Stale translations (D13).** Flag translations older than the default-language text in
   the UI? Needs a per-field, per-language timestamp map (one more JSONB column) or a
   derivation from the audit log (only for 3 months).

Resolved 2026-09-26: verifier execution (D7), role creation on publish (D8), deactivation
stops new runs (D9), catalogue follows `UMP_PUBLIC_PROCESSES` (D10), 3-month retention
(D11), minimal schema (D12), multilingual model cards via language maps (D13).

## Files to create / modify

| File | Action |
|---|---|
| `migrations/versions/0003_create_registry_schema.py` | CREATE |
| `src/ump/core/models/registry/*.py` | CREATE — domain models incl. JSONB shapes and `LocalizedText`, transition graph, `exec_state` mapping, hash, role constants |
| `src/ump/core/interfaces/model_registry.py`, `audit_log.py`, `credential_cipher.py` | CREATE |
| `src/ump/core/services/model_submission.py`, `verification.py`, `interface_drift.py` | CREATE |
| `src/ump/core/services/authorization.py` | MODIFY — `require_platform_role`; `state` rules (D7, D9) |
| `src/ump/core/models/providers_config.py` | MODIFY — `ProcessConfig.state` (default `published`) |
| `src/ump/core/managers/steps/execution_steps.py` | MODIFY — P1: reject unconfigured/excluded processes |
| `src/ump/core/managers/process_manager.py` | MODIFY — listing hides `deactivated`; `in_review` only for verifiers |
| `src/ump/core/interfaces/auth.py` | MODIFY — optional `email`, `name` on `AuthContext` |
| `src/ump/adapters/jwt_auth_adapter.py` | MODIFY — fill the new `AuthContext` fields |
| `src/ump/core/managers/observers.py` | MODIFY — `AuditObserver` |
| `src/ump/adapters/registry_repository_sql.py`, `registry_repository_inmemory.py` | CREATE |
| `src/ump/adapters/audit_log_sql.py`, `audit_log_inmemory.py` | CREATE |
| `src/ump/adapters/credential_cipher_fernet.py` | CREATE |
| `src/ump/adapters/provider_config_db_adapter.py` | CREATE |
| `src/ump/adapters/web/registry_router.py` | CREATE |
| `src/ump/adapters/web/fastapi.py` | MODIFY — include the registry router |
| `src/ump/asgi.py` | MODIFY — wiring, `UMP_PROVIDERS_SOURCE`, drift task in lifespan |
| `src/ump/core/settings.py`, `.env.example` | MODIFY — `UMP_PROVIDERS_SOURCE`, `UMP_REGISTRY_CREDENTIALS_KEY`, `UMP_INTERFACE_CHECK_INTERVAL_SECONDS`, `UMP_REGISTRY_ALLOW_HTTP`, `UMP_REGISTRY_ALLOWED_PRIVATE_HOSTS`, `UMP_REGISTRY_LANGUAGES` |
| `src/ump/cli.py`, `pyproject.toml` | MODIFY — `ump-import-providers`, `ump-retention` commands |
| `charts/urban-model-platform/templates/cronjob-retention.yaml` | CREATE — daily `ump-retention` |
| `tests/test_registry_*.py` | CREATE |
| `docs/diagrams/model-registry-uml.mmd` (+ `.svg`, `.png`) | EXISTS — keep in sync |
| `reports/REF-F12-registry-schema.sql` | EXISTS — reference DDL for 0003; keep in sync |
