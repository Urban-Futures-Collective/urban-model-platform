_Last_updated: 2026-10-29

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
| D2 | **All roles live in Keycloak** (F11): the six platform roles and the model-access roles `{provider}` / `{provider}:{process}`. | The registry stores **no grants**. Whether a user may run a model is decided from the token alone, exactly as today. Access *requests* and their decisions are recorded (D15), but a record never grants anything. |
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
| D13 | **Multilingual model cards** via language maps per field (`{"en": "…", "de": "…"}`), with a per-model default language that is always required. English is the platform default. | No extra table; read API resolves one language with fallback. See [Multilingual content](#multilingual-content). |
| D14 | **Email notifications are derived from the audit log** (decided 2026-10-29). A dispatcher reads new `audit_log` rows and mails the people concerned; no outbox table. | Every audited change can notify without touching the service that makes it; no mail for rolled-back changes. +1 single-row cursor table, +3 columns on `app_user`. See [Notifications](#notifications-phase-e2). |
| D15 | **Access requests: UMP records the workflow, Keycloak holds the grant** (option A, decided 2026-10-29). One table `access_request`; UMP-X's Nitro server maps/unmaps the Keycloak role **first** and then records the decision in UMP with the admin's token. Per model only; the model owner is informed, not asked. | UMP keeps no IdP write path (F11) and D2 stands. Grants made outside the flow (console, groups, provider-wide) are invisible to UMP; a reconciler closes records whose role is gone. Revocation still takes effect at the next token refresh. See [Access requests](#access-requests-phase-e1). |
| D16 | **Closed access requests are deleted after 3 months** (`denied`, `withdrawn`, `revoked`), with the audit log (D11). | Open and granted records are kept as long as they are open. |

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
| `credentials_secret_ref` alternative | `credentials` (encrypted) only | Ops can no longer point at a Kubernetes secret; credentials are entered through onboarding (phase B) and encrypted there. |
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
- audit log for registry actions and job executions;
- access requests for models and the record of their decisions (phase E1; the grant
  itself stays in Keycloak);
- email notifications about registry changes (phase E2).

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
`{"en": "Bike network growth", "de": "Wachstum des Radnetzes"}`. This applies to
`full_name`, `short_description`, `purpose`, `limitations_risks`, `keywords` (a list per
language), `io_notes` values and the text fields inside `applications` and `test_cases`.
Not translated: slug, licence, URLs, `code_ref`, `version_label`, contributor names,
`process_config`.

**Rules.**
- `model.default_lang` (default `en`) is the language that is always complete. The
  database enforces it for `full_name` from creation and for `purpose` before review; the
  core enforces it for all other `L` fields that are filled at all.
- Allowed language codes come from `UMP_REGISTRY_LANGUAGES` (default `en,de`, matching
  UMP-X's i18n). The first entry is the default for new models, so English is the
  default language unless onboarding sets `default_lang` explicitly. The core rejects
  other keys and empty strings; the database only checks the `default_lang` format.
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
- Translations can go stale: editing the English `purpose` does not mark the German one as
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

**Interface hash.** SHA-256 over canonical JSON (sorted keys, no whitespace, UTF-8) of
`{inputs, outputs, jobControlOptions, outputTransmission}`, where the keys `title`,
`description`, `links` and `metadata` are removed from every **definition object** — each
input/output definition and every nested schema object. They are **not** removed where keys
are names rather than keywords: the `inputs`/`outputs` maps themselves and JSON-Schema
`properties`, `patternProperties`, `$defs`, `definitions`. Otherwise an input or schema
property that happens to be called `description` would disappear from the hash. Text edits
on the provider side do not change the hash; schema, cardinality and execution-mode changes
do. The OGC `version` field is **not** used for detection — providers do not reliably bump
it.

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
- Onboarding (`POST`/`PATCH /servers`, phase B) encrypts the credentials it receives.

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
- Role grants/revokes are **not** made by UMP; they are audited by Keycloak admin events
  (see F11). Grants and revokes that go through the access-request flow (phase E1) are
  *recorded* in UMP as `access.grant` / `access.revoke` with the human admin as actor,
  which answers F11's "who granted this" for those grants.

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
- The same run deletes closed access requests (D16):
  `DELETE FROM registry.access_request WHERE status IN ('denied','withdrawn','revoked')
  AND status_changed_at < now() - interval '3 months'` — no guard needed, it is an
  ordinary table.
- Note: 3 months also bounds how far back "who ran what" can be answered from the audit
  log. `public.jobs` has no retention yet and keeps `user_id` + inputs indefinitely — out
  of scope here, but it undercuts the 3-month limit for the same data and should get the
  same rule.

## Access requests (phase E1)

A user who may browse a model but not run it asks for access; an `access-admin` grants or
denies; access can later be revoked. Every step mails the people concerned (E2).

### Division of labour (D15)

**Keycloak stays the single source of truth for roles** (D2, F11). UMP does not write to
Keycloak and does not consult `access_request` on the execution path — execution is still
decided from the token alone. `access_request` is the *workflow and its record*: who asked,
why, who decided, when.

Granting and revoking change Keycloak, so they go through UMP-X's Nitro server, which already
holds the service account (F11):

```
grant:   UMP-X ── POST /admin/access/requests/{id}/grant ──► Nitro
         Nitro ── map {provider}:{process} to user ─────────► Keycloak   (existing mapping = ok)
         Nitro ── POST /access-requests/{id}/transitions ───► UMP        (admin's token)
                  {to_status: granted}                         └─ audit access.grant → E2 mail
revoke:  same order: unmap in Keycloak first, then record `revoked` in UMP
deny / withdraw: no Keycloak change — UMP-X calls UMP directly (/ump/** proxy)
```

**Keycloak first, then UMP.** If the second call fails, the user really has (or has lost)
access and the record merely lags — the admin retries, both calls are idempotent. The
reverse order could mail "access granted" for access that was never mapped.

**What UMP cannot see.** Grants made in the Keycloak console, through groups, provider-wide
(`{provider}`), or revoked there, never reach UMP. Consequences:

- a `granted` record can outlive the role → the **reconciler** (below) closes it;
- an access admin calling the UMP transition directly (bypassing Nitro) only produces a
  wrong *record*, never access; the reconciler corrects it too;
- **provider-wide grants are not requestable** — requests are per model. Provider-wide and
  group grants stay in the F11 matrix.

### Schema (migration 0004)

```sql
CREATE TABLE registry.access_request (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_sub           text        NOT NULL REFERENCES registry.app_user(sub),
    model_id           uuid        NOT NULL REFERENCES registry.model(id),
    status             text        NOT NULL DEFAULT 'requested'
                       CHECK (status IN ('requested','granted','denied','withdrawn','revoked')),
    request_note       text,                     -- the user's reason; NULL for direct grants
    status_note        text,                     -- note of the latest step
    status_changed_by  text,
    status_changed_at  timestamptz NOT NULL DEFAULT now(),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),   -- ETag
    -- nobody decides on their own request
    CHECK (status NOT IN ('granted','denied') OR status_changed_by IS DISTINCT FROM user_sub),
    -- the other side needs the reason
    CHECK (status NOT IN ('denied','revoked') OR status_note IS NOT NULL)
);
-- at most one open record per user and model
CREATE UNIQUE INDEX uq_access_request_open ON registry.access_request(user_sub, model_id)
    WHERE status IN ('requested','granted');
CREATE INDEX idx_access_request_queue ON registry.access_request(status, created_at);
CREATE INDEX idx_access_request_model ON registry.access_request(model_id);
```

- **One row per lifecycle.** A new request after `denied`/`withdrawn`/`revoked` is a new row;
  the step history is in the audit log (D12 pattern, no history table).
- The role is **not stored**: it is `canonical_process_id(provider_server.name,
  model.remote_process_id)`, both immutable.
- No NOTIFY trigger: the table is not part of the provider snapshot.

### Status graph (core, `core/models/registry/access.py`)

```
requested ──► granted ──► revoked
    ├──────► denied
    └──────► withdrawn
```

| Transition | Performed by |
|---|---|
| `requested → withdrawn` | the requester |
| `requested → granted`, `requested → denied` | `access-admin`, not the requester |
| `granted → revoked` | `access-admin` |
| direct grant (new row in `granted`) | `access-admin`, not the user |

Rules (pure core logic, exhaustively tested like `workflow.py`):

- **Requesting** needs an authenticated caller who can see the model (D10 / catalogue
  visibility), a `published` model, and a model that is **not** `anonymous-access`
  (409 otherwise). 409 too if an open record exists, or if the caller's **token already holds**
  `{provider}` or `{provider}:{process}` — the one place the token is consulted, to avoid
  pointless requests.
- **Granting** needs the model to be `published` (409 otherwise). Open requests on a model
  that is reset or deactivated stay open; the admin queue shows the model's state, and the
  admin denies them with a note.
- `denied` and `revoked` need a note (DB CHECK); `granted` takes an optional one.
- The **model owner** is informed (E2), not asked, and may read the requests for their
  model. Making the owner an approver would mean a second decision step — not now.

### API (`/registry/v1`)

| Method | Path | Who |
|---|---|---|
| `POST` | `/models/{id}/access-requests` — `{note?}` | authenticated, see rules |
| `POST` | `/models/{id}/access-grants` — `{user: {sub, email?, name?}, note?}`: record a grant made from the F11 matrix | `access-admin` (called by Nitro after the Keycloak mapping) |
| `GET` | `/access-requests` — `?status=&model_id=&user=&cursor=&limit=`, newest first | `access-admin`: all; model owner: own models; others: own requests (`user` forced to the caller) |
| `GET` | `/access-requests/{id}` | requester, owner of the model, `access-admin`; 404 otherwise |
| `POST` | `/access-requests/{id}/transitions` — `{to_status, note?}` | per the table above; `If-Match` optional (phase C convention) |

Responses carry the model (`id`, `slug`, `full_name` resolved per `?lang=`), the role name,
the user (`sub`, `display_name`, `email` — email only for `access-admin`) and the status
fields.

`/access-grants` upserts the `app_user` row **only if absent**, from the Keycloak data Nitro
passes — the matrix can grant to someone who never logged in to UMP. The next login
overwrites these values from the token as usual.

### Audit actions

`access.request`, `access.withdraw`, `access.grant`, `access.deny`, `access.revoke`, with
`model_id` set and `details = {access_request_id, user_sub, role, note?, direct?: true}`.
Written in the repository transaction like every registry change. Refused requests (409)
are not audited, same as other validation errors.

### Reconciler (UMP-X)

The existing `sync-roles.post.ts` reconciler (F11) gains a second pass, run with the access
admin's token when the access page loads:

1. `GET /access-requests?status=granted` (all pages).
2. For each, check in Keycloak whether the user still holds the role **effectively**
   (`GET /users/{id}/role-mappings/realm/composite`, so group grants count).
3. If not → `POST /access-requests/{id}/transitions {to_status: revoked, note: "role removed
   outside UMP"}`. The user is mailed like for any revoke.

Requests whose user already holds the role (granted through a group meanwhile) are left
open and flagged in the UI ("already has access"); the admin grants (no-op in Keycloak) or
withdraws on their behalf by denying with a note.

### Hexagonal mapping

| Kind | Name | Location |
|---|---|---|
| Model + rules | `AccessRequest`, `AccessStatus`, transition table, `can_request` / `can_transition` | `core/models/registry/access.py` |
| Service | `RegistryAccessService` (request, direct grant, transition, list, read) | `core/services/registry_access.py` |
| Port (extend) | `ModelRegistryPort`: `create_access_request(req, audit)`, `update_access_request(req, expected_updated_at, audit)`, `get_access_request`, `list_access_requests(AccessQuery)`, `ensure_user(user)` | `core/interfaces/model_registry.py` |
| Adapters (extend) | SQL (unique-index violation → 409) and in-memory repositories | `adapters/registry_repository_*.py` |
| Router (extend) | the endpoints above | `adapters/web/registry_router.py` |

No new port and no new adapter; `AuthorizationService` and the execution path are unchanged,
so the OGC contract test is unaffected by construction.

## Notifications (phase E2)

Users are told by email when something that concerns them changes on UMP: a model was
submitted for review, a review was decided, a model drifted, an access request was made or
decided (with phase E1), and so on (D14).

### The audit log is the outbox

Every change worth a mail already writes an `audit_log` row **in the same transaction** as
the change (phase B onwards). Instead of a second, parallel outbox, a dispatcher reads new
audit rows and turns them into mails:

- no service changes — a new mail is one entry in the notification policy;
- no mail for a change that was rolled back, no change without its mail (at-least-once);
- one source for "what happened", so the audit log and the mails cannot disagree.

Costs: mails are keyed to audit action names (renaming an action is a breaking change for
the policy — a test pins the catalogue below), and an event's `details` must carry what the
mail needs (e.g. the target user of an access decision). The audit guard forbids UPDATE, so
delivery state is a **cursor**, not a flag per row.

### Schema (migration 0005)

```sql
CREATE TABLE registry.notification_cursor (
    id                smallint    PRIMARY KEY CHECK (id = 1),   -- single row
    last_audit_id     bigint      NOT NULL,   -- highest audit_log.id fully processed
    failing_audit_id  bigint,                 -- event currently being retried
    attempts          integer     NOT NULL DEFAULT 0,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
-- The migration seeds the row with coalesce(max(audit_log.id), 0): existing history is
-- never mailed.

ALTER TABLE registry.app_user
    ADD COLUMN platform_roles text[] NOT NULL DEFAULT '{}',  -- cache, recipients only
    ADD COLUMN lang           text,                          -- mail language
    ADD COLUMN notify         jsonb  NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(notify) = 'object');             -- {"<category>": false} opt-outs
```

- `platform_roles` caches the six platform roles (`PLATFORM_ROLES`) from the user's last
  token; it is refreshed by the same throttled `touch` that maintains `last_seen_at`. It is
  **only** used to find recipients ("all verifiers", "all access admins") — never for
  authorization, which keeps reading the token. Keycloak remains the only place where roles
  are managed (F11). The cache is stale until the user's next login: a user who lost
  `access-admin` keeps receiving admin mails until then, a new access admin receives none
  until their first login. `UMP_NOTIFY_FALLBACK_ADMIN_EMAILS` (a shared mailbox) covers the
  second case.
- `lang` comes from the token's `locale` claim (`AuthContext.locale`, new optional field),
  restricted to `UMP_REGISTRY_LANGUAGES`; otherwise the first configured language.
- `notify` holds opt-outs per category (`review`, `access`, `drift`, `ownership`, `jobs`).
  Absent = default for that category (all on, except `jobs`).

### Core

| Kind | Name | Notes |
|---|---|---|
| Model | `PlannedMail(kind, category, targets, params)`, `Target(sub \| role \| email)`, `ModelRef` | `core/models/registry/notifications.py` |
| Pure function | `plan(record: AuditRecord, model: ModelRef \| None, fallback_admin_emails) -> list[PlannedMail]` | The catalogue below. Recipients are *targets* — a user, everyone holding a platform role, or a plain address — resolved by the dispatcher, so the policy needs no port. The actor is never notified of their own action; `outcome != success` never notifies — except `job.complete`, which is addressed to its actor and also reports failed jobs. |
| Service | `NotificationDispatcher` (`core/services/notifications.py`) | Reads audit rows after the cursor, plans, resolves targets (`get_users`, `list_users_with_role`), filters (actor, missing email, `notify`), renders per recipient language, sends, advances the cursor per event. One mail per address and event. |
| Service | `NotificationScheduler` | Lifecycle context, leader election and backoff (below). |
| Port | `MailerPort` | `async send(EmailMessage(to, subject, text))`; raises `TransientMailError` / `PermanentMailError`. Plain text only. |
| Port | `NotificationRendererPort` | `render(kind, lang, params) -> RenderedMail(subject, text)`. Presentation, so the implementation is an adapter. |
| Port (extend) | `AuditLogPort` | `list_after(after_id, occurred_before, limit)` (ascending), `get_notification_cursor`, `save_notification_cursor`. |
| Port (extend) | `ModelRegistryPort` | `list_users_with_role(role)`, `set_notify(sub, notify)`; `upsert_user` refreshes `platform_roles`/`lang` only when given. |

**Dispatcher loop.** Runs as a `lifecycle` context like `InterfaceCheckScheduler`; leader
election through `PollLockPort` with key `uuid5(NAMESPACE_URL, "ump:registry:notifications")`
(the leader keeps the lock until shutdown). Every `UMP_NOTIFY_INTERVAL_SECONDS` (default
30, `0` disables) it processes up to 100 events in id order:

- all recipients sent (or skipped: no email, opted out, permanent error) → cursor advances;
- transient error (SMTP down, timeout) → stop, keep cursor, `attempts += 1`, retry next
  tick with exponential backoff (cap 15 min);
- after `UMP_NOTIFY_MAX_ATTEMPTS` (default 10) the event is skipped and logged, so one bad
  event cannot block the queue. Recipients that already got the mail before a retry may get
  it twice (at-least-once).

Audit ids come from an identity column and can become visible out of order (a transaction
that took an id earlier commits later). The dispatcher therefore only reads rows older than
`UMP_NOTIFY_SETTLE_SECONDS` (default 5), which bounds the gap to transactions that run
longer than that — acceptable for notifications.

Recipients are resolved **at send time**, not at event time: an owner transferred in
between gets the mail. Retention (3 months) is irrelevant as long as the dispatcher is not
months behind.

### Adapters

| Adapter | Implements | Notes |
|---|---|---|
| `adapters/mail/smtp_mailer.py` | `MailerPort` | stdlib `smtplib` via `asyncio.to_thread` (no new dependency); STARTTLS / SSL, optional login |
| `adapters/mail/log_mailer.py` | `MailerPort` | Writes the mail to the log; dev and tests |
| `adapters/mail/jinja_renderer.py` | `NotificationRendererPort` | Jinja2 (already a dependency); one template per kind and language, `adapters/mail/templates/{lang}/{kind}.txt`, with blocks `subject` and `body`; `StrictUndefined`; unknown languages fall back to the first platform language. Links into UMP-X (`/models/{id}`, `/admin/access`, `/jobs/{id}`) are built only in the templates, so matching UMP-X's routes is a template edit. |

`UMP_MAIL_BACKEND=none|log|smtp` (default `none`: no dispatcher is started). Mailpit is
added to `docker-compose-dev.yaml` for local SMTP. Links in mails point to UMP-X
(`UMP_FRONTEND_BASE_URL`); without it mails carry no links.

### Catalogue

| Audit action | Category | Recipients |
|---|---|---|
| `model.status.change` → `submitted` | `review` | all `verifier`s (except the owner) |
| `model.status.change` → `in_review` (picked up, or reset), `changes_requested`, `rejected`, `verified`, `published`, `deactivated` | `review` | owner (incl. the note) |
| `model.reverify` | `review` | owner |
| `model.interface.drifted`, `model.interface.unreachable` | `drift` | owner + all `verifier`s |
| `model.interface.restored` | `drift` | owner |
| `model.owner.transfer` | `ownership` | previous and new owner |
| `access.request` | `access` | all `access-admin`s (+ `UMP_NOTIFY_FALLBACK_ADMIN_EMAILS`) and the model owner |
| `access.grant` | `access` | the user — the mail says access is active after the next login / token refresh |
| `access.deny`, `access.revoke` | `access` | the user (incl. note) |
| `job.complete` | `jobs` | job owner — **opt-in** (`notify.jobs = true`), off by default |

Not notified: `submitted → draft` (a withdrawal before anyone picked the model up — the
earlier draft of this table named "the verifier who picked it up", but a `submitted` model
has not been picked up yet), `access.withdraw`, card edits, server changes, discovery, `job.execute`, periodic
`model.interface.changed` on never-verified models.

### API

| Method | Path | Role |
|---|---|---|
| `GET` | `/me` — gains `notify` (effective settings per category) and `lang` | any authenticated user |
| `PATCH` | `/me/notifications` — `{"<category>": true\|false}` | the user |

### Settings

`UMP_MAIL_BACKEND`, `UMP_MAIL_FROM`, `UMP_SMTP_HOST`, `UMP_SMTP_PORT`, `UMP_SMTP_USER`,
`UMP_SMTP_PASSWORD`, `UMP_SMTP_SECURITY` (`starttls|ssl|none`), `UMP_FRONTEND_BASE_URL`,
`UMP_NOTIFY_INTERVAL_SECONDS`, `UMP_NOTIFY_SETTLE_SECONDS`, `UMP_NOTIFY_MAX_ATTEMPTS`,
`UMP_NOTIFY_FALLBACK_ADMIN_EMAILS`.

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

Selection: `UMP_PROVIDERS_SOURCE=file|db` (default `file`). Migration path (decided
2026-10-19): **no automatic import of `providers.yaml`.** Servers and models are entered
through the registry API (phase B onboarding) and go through review; for local
development `scripts/seed_registry_dev.sql` registers the bundled mock server.

**Composition root (`asgi.py`):** construct the SQL (or in-memory) registry adapter, the
audit adapter, the cipher, the providers adapter selected by `UMP_PROVIDERS_SOURCE`; inject
them into the new services; attach `AuditObserver`; start the drift-check task in the
lifespan, guarded by `PollLockPort`.

## API sketch (`/registry/v1`)

UMP extension, not OGC. JSON only, no trailing slashes (F1 conventions).

Decided 2026-10-19:

- **Mounted at `/registry/v{major}`** (`UMP_REGISTRY_API_VERSIONS`, default `["1"]`), outside
  the OGC version loop — same reasoning as `/mcp/v1` (F10): `/v1.0` is the OGC standard's
  number, the registry contract is UMP's own. **The OGC API Processes endpoints do not
  change**: the registry reaches them only through the `ProvidersPort` snapshot (status →
  `exec_state` → `ProcessConfig.state`). A test pins the OpenAPI of every `/v1.0/*` path.
- **Audit rows are written by the repository method that makes the change**, in the same
  transaction. The core still decides the content: services build an `AuditEvent` and pass
  it to the write method (e.g. `update_model(model, audit=event)`). No unit-of-work port.
- **Per-user LLM key** (v4 migration, with projects): stored encrypted via
  `CredentialCipherPort`. `PUT`/`DELETE /me/llm-key`, `GET /me/llm-key` → `{set, provider,
  updated_at}`; plus one owner-only plaintext read that is **called only by UMP-X's Nitro
  server**, which relays the user's chat to the LLM — the key never reaches the browser.
  Every plaintext read is audited (without the key).

Delivery phases: **A** read + identity (`/me`, `/models`, `/servers`, `/roles/vocabulary`)
· **B** onboarding (server create/edit, discover, drafts, card edits) · **C** workflow
(transitions, reverify, owner transfer) · **D** drift + audit (`/interface`, `/rediscover`,
`/audit`) · **E** access requests (E1) and email notifications (E2, see
[Notifications](#notifications-phase-e2)) · **later, with v4**: `/me/llm-key`, projects.

| Method | Path | Role |
|---|---|---|
| `GET` | `/me` — upserts `app_user` from the token, returns profile + platform roles (+ `notify`, `lang`, phase E2) | any authenticated user |
| `PATCH` | `/me/notifications` (phase E2) — per-category opt-in/out | the user |
| `PUT` · `DELETE` · `GET` | `/me/llm-key` (v4) — `GET` returns `{set, provider, updated_at}` only | the user |
| `GET` | `/me/llm-key/secret` (v4) — plaintext, audited; called by UMP-X's Nitro server only | the user |
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
| `POST` | `/models/{id}/access-requests` (phase E1) | authenticated, see [Access requests](#access-requests-phase-e1) |
| `POST` | `/models/{id}/access-grants` (phase E1) — record a matrix grant | `access-admin` (via Nitro) |
| `GET` | `/access-requests`, `/access-requests/{id}` (phase E1) | `access-admin`; owner (own models); requester (own) |
| `POST` | `/access-requests/{id}/transitions` (phase E1) | requester (`withdrawn`), `access-admin` (rest; `granted`/`revoked` via Nitro) |

## Migrations

`migrations/versions/0003_create_registry_schema.py`, written by hand like `0001`. Reference
DDL: [`REF-F12-registry-schema.sql`](REF-F12-registry-schema.sql) — reflects D1–D12 and was
tested on PostgreSQL 16 (4 tables, credential rule, generated `exec_state`, four-eyes and
card-completeness checks, default-language checks and fallback, keyword search per
language, JSONB type checks, provider view, audit guard and retention delete). The migration can execute it largely verbatim via `op.execute`.

## Implementation status

**Phase 0 (P1) and phase 2 are implemented**, plus the slice of phase 1 the import needs
(branch `v3.0.0alpha/providers-db-adapter`):

- **P1** — `ValidateAndResolveStep` halts with 404 for unconfigured/excluded processes (bare
  ids resolve only to configured processes; the bare-id fallback is gone) and with 403
  `Model deactivated` for deactivated ones.
- **State rules (D7, D9)** — `ProcessConfig.state` (default `published`);
  `AuthorizationService` refuses `deactivated` before the anonymous-access shortcut, admits
  only `verifier` for `in_review`, and admits `verifier` for `published` too.
  `can_list_process` hides `in_review` from non-verifiers in `GET /processes[/{id}]`;
  `ProcessManager` never lists or describes `deactivated` processes.
- **`ProviderConfigDbAdapter`** (`UMP_PROVIDERS_SOURCE=db`) — in-memory `ProvidersSnapshot`
  (core; also used by the file adapter) rebuilt on startup, on `ump_providers_changed`
  (triggers added to migration 0003), every `UMP_PROVIDERS_DB_POLL_INTERVAL` seconds as
  fallback, and on `load_providers()`. Entered through the new `create_app(lifecycle=…)`
  hook; one engine per worker is shared with the job repository.
- **Credentials** — `CredentialCipherPort` + `FernetCredentialCipher`.
  `UMP_REGISTRY_CREDENTIALS_KEY` takes comma-separated keys (first encrypts, all decrypt —
  `MultiFernet`); this replaces the key-id prefix idea. The stored secret is the encrypted
  JSON of the auth fields; `auth_config_to_secret` / `auth_config_from_secret` map it.
- **No `providers.yaml` import** (dropped 2026-10-19; it existed briefly as
  `ump-import-providers`). `scripts/seed_registry_dev.sql` seeds the mock server for local
  work and for the Postgres tests of `ProviderConfigDbAdapter`. Servers with credentials
  cannot be seeded in plain SQL — the ciphertext needs `UMP_REGISTRY_CREDENTIALS_KEY`.

**Registry API phase A is implemented** (branch `v3.0.0alpha/registry-api`): `GET /registry/v1`
`/me`, `/models`, `/models/{id}`, `/servers`, `/servers/{id}`, `/roles/vocabulary`.

- Router `adapters/web/registry_router.py`, mounted per `UMP_REGISTRY_API_VERSIONS` outside the
  OGC loop. Core services `RegistryCatalogService`, `RegistryServerService`,
  `RegistryAccountService`; views in `core/models/registry/views.py`; role constants in
  `core/models/registry/roles.py`; `AuthorizationService.require_platform_role`.
- Catalogue visibility: `published` for everyone allowed to browse (D10); verifiers also see
  every non-draft status, platform admins everything, owners all of their own; anything else
  is 404. Review details (`status_note`, `verified_*`) only for owner / verifier / admin.
- `ModelQuery` separates visibility (`visible_statuses` OR `visible_owner`) from filters;
  `ModelQuery.matches` is the reference semantics, and a Postgres test checks the SQL adapter
  against it. Keyset pagination by slug (`cursor`, `limit` ≤ 200).
- `app_user` is upserted on `/me` and just-in-time on any authenticated registry request,
  throttled per worker (`touch`); `AuthContext` gained optional `email` / `name`.
- `ProcessManager` reuses a provider's cached process list only while its listable process ids
  are unchanged, so a newly published model shows up in `/v1.0/processes` at once.
- `tests/test_ogc_contract_unchanged.py` pins the `/v1.0/*` OpenAPI (paths + referenced
  schemas) to a snapshot generated from commit 511a7e9, i.e. before the registry work.
- Without a database the registry uses the in-memory repository (dev only).

**Registry API phase B is implemented** (branch `v3.0.0alpha/registry-api-phase-b`):
`POST /servers`, `PATCH /servers/{id}`, `POST /servers/{id}/discover`,
`POST /servers/{id}/models`, `PATCH /models/{id}`.

- Services `RegistryOnboardingService` (servers, discovery, drafts) and `RegistryCardService`
  (card edits); input models and JSONB shapes in `core/models/registry/card.py` (language maps
  restricted to `UMP_REGISTRY_LANGUAGES`, no empty texts, SPDX licence, contributor roles, …).
- **Audit:** services build an `AuditEvent` (actor, role snapshot, request id from the
  correlation id, details without secrets, before/after diff for card edits) and pass it to
  the repository write, which stores both in one transaction — tested against Postgres for
  success, unique conflicts, stale updates and all-or-nothing batches.
- **Concurrency:** `ETag` on `GET`/`PATCH` of servers and models, derived from `updated_at`;
  `PATCH` needs `If-Match` (428 without, 412 if stale; `*` is not accepted). Repositories
  update conditionally on `updated_at`.
- **Credentials:** encrypted before the repository; `authentication: null` switches to NoAuth
  and deletes the secret; 503 if `UMP_REGISTRY_CREDENTIALS_KEY` is missing.
- **Outbound safety — deviation from "no new outbound HTTP port":** discovery uses a new
  `SafeFetchPort` (`adapters/safe_fetch_aiohttp.py`). The checks must run when connecting —
  a resolver wrapper drops non-public addresses, so a DNS answer cannot change between check
  and use — and the execution client (`HttpClientPort`) follows redirects and has no size cap.
  Settings: `UMP_REGISTRY_ALLOW_HTTP`, `UMP_REGISTRY_ALLOWED_PRIVATE_HOSTS`,
  `UMP_REGISTRY_FETCH_TIMEOUT`, `UMP_REGISTRY_FETCH_MAX_BYTES`.
- **Drafts:** pre-filled from the raw description (title → `full_name`, description,
  keywords, version, interface hash in `current_interface_sha256`); a translation is stored
  only if the server answers `Accept-Language` differently. Slug collisions get `-2`, `-3`, …
  Existing models are reported, not duplicated.
- **Card rules:** owner only (`provider` role); 403 if the caller can see the model, else 404;
  no edits while `deactivated`; the 0003 invariants are checked first with readable errors.
  **`process_config` (execution settings such as `anonymous-access`) is editable only in
  `draft` and `changes_requested`** — it changes who may run the model, so on a model in
  review or published it must go through review. (Decision taken in implementation; revisit
  with open question 3.)
- Writes that can change the provider snapshot call `ProvidersPort.load_providers()`.
- Writes need an authenticated caller, so `UMP_AUTH_ENABLED=false` rejects them (401).

Known limitation: the execution path (`HttpClientPort`) is not address-checked, so a server
whose DNS later points at an internal address would be reached by forwarded executions. The
drift check (phase D) re-validates `base_url` periodically; pinning execution traffic would
need the guarded resolver in `AioHttpClientAdapter` too.

**Registry API phase C is implemented** (branch `v3.0.0alpha/registry-api-phase-c`):
`POST /models/{id}/transitions`, `POST /models/{id}/reverify`, `PATCH /models/{id}/owner`.

- The status graph and role rules are pure core logic (`core/models/registry/workflow.py`,
  exhaustively tested): `owner` steps need the owner holding `provider`; `reviewer` steps a
  `verifier` who is not the owner (four-eyes, also for an owner who holds `verifier`);
  deactivation a `verifier` or `platform-admin`.
- Choices taken in implementation: **a note is required** for `changes_requested`,
  `rejected`, the `published → in_review` reset and every deactivation (the other side needs
  the reason); **the card must be complete** (`purpose` in `default_lang`, `license`) already
  at `submitted`, so the owner learns it when submitting rather than at pick-up;
  `status_note` holds the note of the latest step.
- **Verification job check (decided 2026-10-19):** `verified` and `reverify` need a
  `verification_job_id` that exists in UMP's job store, was started by the verifier, ran
  this model (`{provider}:{process}`), finished `successful`, and was started after the
  current review round began (after the last verification, for `reverify`).
- On `verified` / `reverify` the current raw description is fetched through `SafeFetchPort`
  **in the model's default language** (the hash is defined on that response) and stored as
  `verified_interface` + hash; `interface_state = in_sync`.
- `published_at` is set on the first publication only (role vocabulary, D8). Steps that
  change `exec_state` reload the provider snapshot, so `/v1.0` sees publish, reset and
  deactivation at once. `If-Match` is optional on the POSTs and required on `PATCH /owner`.
- Owner transfer (platform admin) refuses a new owner who verified the model (four-eyes).
- A Postgres end-to-end test runs register → discover → drafts → card edit → submit →
  review → verify → publish on the real adapters; every write passes the 0003 CHECKs and the
  model appears in `v_providers_config`.

**Registry API phase D is implemented** (branch `v3.0.0alpha/registry-api-phase-d`):
`GET /models/{id}/interface`, `POST /servers/{id}/rediscover`, `GET /audit`, the periodic
drift check and `AuditObserver`.

- `InterfaceDriftService` (`core/services/interface_drift.py`) checks every model with a
  non-NULL `exec_state` on an active server, through `SafeFetchPort` (so `base_url` is
  re-validated each time) in the model's default language. Outcomes as in the table above.
  Choices taken in implementation: **a model that was never verified is `in_sync`** (there is
  nothing to drift from) but a hash change is audited as `model.interface.changed`; a
  drifted model that drifts *further* is audited again; `unreachable` keeps the previous
  `current_interface_sha256`; recovery from `unreachable` is `model.interface.restored`.
  Periodic events have `actor_sub = NULL`; rediscover events carry the caller.
- **Results do not touch `updated_at`.** A new port method `record_interface_checks` writes
  only `interface_state`, `current_interface_sha256`, `last_checked_at` — otherwise an
  hourly check would change every ETag and fail the next card `PATCH` with 412. Each write is
  conditional on the verified hash the check compared against, so a re-verification that
  lands during a check wins. One transaction per run → one `ump_providers_changed` NOTIFY.
- `InterfaceCheckScheduler` runs as a `lifecycle` context (`UMP_INTERFACE_CHECK_INTERVAL_SECONDS`,
  default 3600, `0` disables; first run after 60 s). **Leader election:** the worker that gets
  the `PollLockPort` lock (key `uuid5(NAMESPACE_URL, "ump:registry:interface-check")`) keeps
  it until shutdown; the others retry every interval. With a database the lock is always
  `PgAdvisoryPollLock`, also when the job store is in memory.
- `GET /models/{id}/interface` (owner, `verifier`, `platform-admin`; 404 if not visible) is
  read-only: stored state plus a live fetch, its hash, `matches_verified` and a diff summary
  (`core/models/registry/interface_diff.py`: inputs/outputs added/removed/changed, changed
  `jobControlOptions`/`outputTransmission` — computed on the canonical interface, so text
  edits never show). An unreachable server is reported in `live_error`, not as an error status.
- `POST /servers/{id}/rediscover` (owner or `platform-admin`, 404 otherwise) checks that
  server's models now and audits `server.rediscover`.
- **Audit log port:** `AuditLogPort` (`append`, `list_events(AuditQuery)`) with
  `SQLAuditLog` / `InMemoryAuditLog`. Registry changes still write their audit row in the
  repository transaction; the port is for events without a registry change (jobs) and for
  reads. `AuditLogRecord` moved to `adapters/audit_log_sql.py`. The in-memory registry
  repository appends to an injected `InMemoryAuditLog`, so `/audit` works without a database.
- `GET /audit` (`verifier`, `access-admin`, `platform-admin`): newest first, filters
  `model_id`, `actor`, `action`, `job_id`, `since` (incl.), `until` (excl.); cursor = last id.
- `AuditObserver` records `job.execute` (on create) and `job.complete` (terminal;
  `outcome = failure` only for `failed`). `actor_roles` is empty — a job carries no token —
  and `model_id` is not resolved (the process id is in `details`).

**Registry API phase E1 is implemented** (branch `v3.0.0alpha/registry-api-phase-e`): migration 0004 (`access_request`), `POST /models/{id}/access-requests`,
`POST /models/{id}/access-grants`, `GET /access-requests[/{id}]`,
`POST /access-requests/{id}/transitions` — as specified in
[Access requests](#access-requests-phase-e1).

- Status graph and actor rules: `core/models/registry/access.py` (exhaustively tested);
  service `RegistryAccessService` (`core/services/registry_access.py`); repository methods
  on `ModelRegistryPort` (SQL + in-memory): `ensure_user`, `get_users`,
  `get_access_request`, `list_access_requests(AccessQuery)`, `create_access_request`,
  `update_access_request`. `AccessQuery.matches` is the reference semantics; a Postgres
  test checks the SQL query (visibility, filters, keyset order) against it.
- Updates write only the step columns (`status`, `status_note`, `status_changed_by/at`);
  identity and `request_note` are never rewritten. Optimistic concurrency on `updated_at`;
  `If-Match` optional on the transition POST (phase C convention), `ETag` on every response.
- List order is newest first, `(created_at, id)` desc; the cursor encodes that pair.
- `AuthorizationService` and the execution path are untouched; the OGC contract test is
  unchanged.
- Not part of UMP: the UMP-X routes and the reconciler pass (F11).

**Registry API phase E2 is implemented** (branch `v3.0.0alpha/registry-api-phase-e2`, on
top of E1): migration 0005, the dispatcher, mail adapters and `PATCH /me/notifications`, as
specified in [Notifications](#notifications-phase-e2).

- Off by default (`UMP_MAIL_BACKEND=none`); `log` writes mails to the log, `smtp` sends them
  (stdlib `smtplib` in a thread, one connection per mail). Wired in `asgi.py` as a second
  lifecycle task; the drift check and the dispatcher share one `PollLockPort` instance
  (`registry_lock`) with different keys.
- Choices taken in implementation: the owner is also told when a verifier **picks up** the
  model (`submitted → in_review`); `submitted → draft` notifies nobody (see the catalogue
  note); a template error skips that mail, not the queue; renderer and mailer are plain
  text only (no HTML part).
- `AuthContext.locale` comes from the token's `locale` claim; `mail_language` maps
  `de-CH` → `de` and ignores languages outside `UMP_REGISTRY_LANGUAGES`.
- `/me` gains `lang` and `notify` (effective per category); `PATCH /me/notifications` merges
  `{category: bool}` (400 on unknown categories).
- Mailpit in `docker-compose-dev.yaml` publishes SMTP on host port 1025; if that port is
  taken locally, remap it.

Not yet: retention (`ump-retention` + CronJob; it will also delete closed access requests,
D16).

## Implementation phases

0. **Prerequisite P1.** Reject unconfigured/excluded processes on execution. Ships
   independently.
1. **Schema + read side.** Migration 0003, domain models, `ModelRegistryPort` (SQL +
   in-memory), `GET /models`. (The `providers.yaml` import was dropped, see above.)
2. **Providers from the database.** `ProviderConfigDbAdapter`, `UMP_PROVIDERS_SOURCE`.
   Existing OGC behaviour must be unchanged — the existing test suite is the acceptance test.
3. **Onboarding + model cards.** Servers, discovery, pre-fill, credentials, outbound checks.
4. **Workflow.** Transitions, verification, platform-role checks, audit log.
5. **Drift (policy A).** Periodic check, state exposure, `AuditObserver`.
6. **Retention.** `ump-retention`, CronJob in the Helm chart.
7. **Access requests (E1), done.** Migration 0004 (`access_request`), status graph, service,
   repository methods, endpoints; UMP-X Nitro routes and reconciler (F11).
8. **Notifications (E2), done.** Migration 0005 (`notification_cursor`, `app_user` columns),
   policy, dispatcher, mail adapters, `/me/notifications`.

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
  `minOccurs`, changed schema type → different hash; key order irrelevant; an input or a
  schema property **named** `description` is kept;
- drift service with an in-test `HttpClientPort`: in_sync → drifted → restored;
  unreachable; audit event only on state change;
- `ProviderConfigDbAdapter` produces `ProviderConfig` equal to what the file adapter produces
  for the same configuration (seeded rows vs. the expected `ProviderConfig`);
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
  rejected;
- access status graph: every allowed transition passes, every other pair is rejected;
  requester cannot grant/deny own request (core and CHECK); deny/revoke without note
  rejected; request refused for anonymous-access, unpublished or invisible models, with an
  open record, or when the token already holds `{provider}` / `{provider}:{process}`;
  grant refused once the model is no longer published;
- access API authorization matrix: requester / owner / `access-admin` / other per endpoint;
  `GET /access-requests` never leaks other users' requests; email only for `access-admin`;
- direct grant upserts an absent `app_user` and never overwrites an existing one;
- Postgres: partial unique index (second open request → 409, new request after denial ok);
  audit row written in the same transaction; retention deletes closed rows older than
  3 months and keeps open ones;
- execution is unaffected by `access_request` by construction: `AuthorizationService`
  has no registry dependency, and the OGC contract test pins `/v1.0`;
- notification policy (table-driven over the catalogue): recipients per action and target
  status; actor never notified; `denied`/`failure` outcomes never notify; unknown actions
  produce nothing; the catalogue's action names exist in the code;
- dispatcher with an in-test `MailerPort` and in-memory audit log: cursor advances after
  success; transient error keeps the cursor and retries; skip after max attempts; opted-out
  and email-less recipients skipped; rows younger than the settle time not read; a second
  worker without the lock sends nothing;
- renderer: every catalogue kind has a template in every `UMP_REGISTRY_LANGUAGES` language;
  fallback to the first language;
- Postgres: migration 0005 seeds the cursor at the current max id (history not mailed);
  end-to-end submit → verifier mail, review decision → owner mail.

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
5. **Owner as approver (D15).** Should a model owner be able to approve requests for their
   own model (two-step: owner → access admin), or stay informed only?
6. **Recipient cache (D14).** Is the `app_user.platform_roles` cache (stale until next
   login) acceptable, or should admin/verifier mails go only to configured mailboxes?
7. **Digest mails.** One mail per event can be noisy for verifiers and access admins; a
   daily digest per category would need per-recipient state (one more table or JSONB on
   `app_user`).

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
| `src/ump/cli.py`, `pyproject.toml` | MODIFY — `ump-retention` command |
| `charts/urban-model-platform/templates/cronjob-retention.yaml` | CREATE — daily `ump-retention` |
| `migrations/versions/0004_access_requests.py` | DONE — `access_request` (E1) |
| `migrations/versions/0005_notifications.py` | DONE — `notification_cursor`, `app_user` columns (E2) |
| `src/ump/core/models/registry/access.py`, `src/ump/core/services/registry_access.py` | DONE — access requests (E1) |
| `src/ump/core/interfaces/model_registry.py`, `src/ump/adapters/registry_repository_*.py`, `src/ump/adapters/web/registry_router.py` | DONE — access-request methods and endpoints (E1) |
| `src/ump/core/models/registry/notifications.py`, `src/ump/core/services/notifications.py` | DONE — policy (`plan`), `NotificationDispatcher`, `NotificationScheduler` (E2) |
| `src/ump/core/interfaces/mailer.py`, `notification_renderer.py` | DONE — `MailerPort`, `NotificationRendererPort` (E2) |
| `src/ump/adapters/mail/` | DONE — `smtp_mailer.py`, `log_mailer.py`, `jinja_renderer.py`, `templates/{en,de}/` (E2) |
| `docker-compose-dev.yaml` | DONE — Mailpit (E2) |
| `tests/test_registry_*.py` | CREATE |
| `docs/diagrams/model-registry-uml.mmd` (+ `.svg`, `.png`) | EXISTS — keep in sync |
| `reports/REF-F12-registry-schema.sql` | EXISTS — reference DDL for 0003; keep in sync |
