-- =============================================================================
-- UMP Model Registry — reference DDL (PostgreSQL >= 14)            REF-F12
-- =============================================================================
--
-- Reference for Alembic migration 0003_create_registry_schema.
-- Spec and decisions: reports/REF-F12-model-registry.md
-- Diagram:            docs/diagrams/model-registry-uml.mmd
--
-- Four tables in schema `registry`, next to the unchanged public.jobs:
--   app_user         accounts seen by UMP (cache of token claims)
--   provider_server  one OGC API Processes server = one providers.yaml entry
--   model            one remote process: model card + execution config +
--                    review status + verified interface + drift state
--   audit_log        append-only activity log, 3-month retention
--
-- Design rules (see REF-F12 "Simplification")
--   * Roles and access live in Keycloak only (D2). No grants, no visibility.
--   * Descriptive model-card parts that are only displayed, never joined or
--     filtered (contributors, literature, applications, test cases,
--     requirements, I/O notes, execution config) are JSONB documents,
--     validated by Pydantic models in the core.
--   * Free-text model-card fields are multilingual (D13): JSONB language maps
--     {"de": "…", "en": "…"} with a per-model default language that must
--     always be present. Allowed language codes are a platform setting
--     (UMP_REGISTRY_LANGUAGES), checked in the core.
--   * Workflow rules (allowed status transitions, who may perform them) live in
--     the core. The database only keeps invariants that are cheap to state as
--     CHECK constraints.
--   * No triggers except the audit log guard.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS registry;


-- -----------------------------------------------------------------------------
-- app_user — one row per person who has authenticated against UMP.
-- Upserted just-in-time from the token. No password, no roles.
-- PK is the JWT `sub` (Keycloak user id), the same value as public.jobs.user_id.
-- People named on a model card who have no account are plain entries in
-- model.contributors, not rows here.
-- -----------------------------------------------------------------------------
CREATE TABLE registry.app_user (
    sub             text        PRIMARY KEY,
    display_name    text,
    email           text,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now()
);


-- -----------------------------------------------------------------------------
-- provider_server — replaces a `providers:` entry in providers.yaml.
-- `name` prefixes every process id and every Keycloak model-access role, so the
-- core treats it as immutable after creation.
-- -----------------------------------------------------------------------------
CREATE TABLE registry.provider_server (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                 text        NOT NULL UNIQUE
                         CHECK (name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    base_url             text        NOT NULL,          -- OGC API Processes landing page
    owner_sub            text        REFERENCES registry.app_user(sub),  -- NULL for imported entries
    auth_type            text        NOT NULL DEFAULT 'NoAuth'
                         CHECK (auth_type IN ('NoAuth', 'BasicAuth', 'ApiKey', 'BearerToken')),
    credentials          bytea,      -- encrypted by CredentialCipherPort (key-id prefixed); write-only
    ttw_job_done_seconds numeric(10,1) NOT NULL DEFAULT 1500,
    is_active            boolean     NOT NULL DEFAULT true,
    last_discovered_at   timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CHECK ((auth_type = 'NoAuth') = (credentials IS NULL))
);


-- -----------------------------------------------------------------------------
-- model — one remote process on one provider server.
--
-- Status workflow (transitions enforced in the core, VerificationService):
--   draft → submitted → in_review → verified → published
--   in_review → changes_requested → submitted      in_review → rejected
--   published → in_review (reset)                  any runnable → deactivated
-- Metadata edits by the owner do not change the status. Re-verifying a
-- published model (e.g. after interface drift) updates the verified_* columns
-- in place; only an explicit reset takes it back to in_review.
--
-- exec_state is what the execution path sees (D7, D9), exposed to the core as
-- ProcessConfig.state:
--   published    → model-access roles, anonymous-access, or `verifier`
--   in_review    → `verifier` only
--   deactivated  → nobody may start a run; entry stays so running jobs can
--                  still poll and fetch results
--   NULL         → never reached review: not in the provider snapshot
-- -----------------------------------------------------------------------------
CREATE TABLE registry.model (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                text        NOT NULL UNIQUE
                        CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    provider_server_id  uuid        NOT NULL REFERENCES registry.provider_server(id),
    remote_process_id   text        NOT NULL,
    owner_sub           text        NOT NULL REFERENCES registry.app_user(sub),

    -- model card ---------------------------------------------------------------
    -- L = language map {"<lang>": "<text>"} (D13); the default language is mandatory.
    default_lang        text        NOT NULL DEFAULT 'de'
                        CHECK (default_lang ~ '^[a-z]{2,3}(-[A-Z]{2})?$'),   -- BCP 47 subset
    full_name           jsonb       NOT NULL,                  -- L
    short_description   jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- L
    purpose             jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- L
    limitations_risks   jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- L
    keywords            jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- {"<lang>": ["…", …]}
    version_label       text,                              -- informational
    repository_url      text,
    code_ref            text,                              -- tag / commit / package version
    license             text        CHECK (license ~ '^[A-Za-z0-9.+:()\- ]+$'),  -- SPDX expression
    requirements        jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- {cpu_cores, memory_gb, gpu, storage_gb, runtime_seconds, software}
    contributors        jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- [{name, role, orcid, affiliation, email}]
    literature          jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- [{citation, doi, url, year}]
    applications        jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- [{title: L, description: L, location, url, year}]
    test_cases          jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- [{name, description: L, input_url, expected_output_url, sha256…}]
    io_notes            jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- {"<ogc input/output id>": L}

    -- execution config (the providers.yaml process entry) ------------------------
    process_config      jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- anonymous-access, result-storage, poll-interval, …

    -- lifecycle --------------------------------------------------------------------
    status              text        NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'submitted', 'in_review', 'changes_requested',
                                          'verified', 'published', 'rejected', 'deactivated')),
    exec_state          text        GENERATED ALWAYS AS (
                            CASE status
                                WHEN 'published'         THEN 'published'
                                WHEN 'in_review'         THEN 'in_review'
                                WHEN 'verified'          THEN 'in_review'
                                WHEN 'changes_requested' THEN 'deactivated'
                                WHEN 'rejected'          THEN 'deactivated'
                                WHEN 'deactivated'       THEN 'deactivated'
                            END) STORED,
    status_note         text,                              -- last review comment / rejection / deactivation reason
    status_changed_by   text        REFERENCES registry.app_user(sub),
    status_changed_at   timestamptz,
    published_at        timestamptz,                       -- first publication; model-access roles exist from then on

    -- verification: "verified against this interface, at this time" ---------------
    verified_by         text        REFERENCES registry.app_user(sub),
    verified_at         timestamptz,
    verification_job_id text,                              -- public.jobs.id of the verifier's run
    verified_interface  jsonb,                             -- raw OGC process description at verification
    verified_interface_sha256 text  CHECK (verified_interface_sha256 ~ '^[0-9a-f]{64}$'),

    -- interface drift, policy A: flag only (D5) ------------------------------------
    current_interface_sha256 text   CHECK (current_interface_sha256 ~ '^[0-9a-f]{64}$'),
    interface_state     text        NOT NULL DEFAULT 'unchecked'
                        CHECK (interface_state IN ('unchecked', 'in_sync', 'drifted', 'unreachable')),
    last_checked_at     timestamptz,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (provider_server_id, remote_process_id),
    -- four-eyes: the owner never verifies their own model
    CHECK (verified_by IS NULL OR verified_by <> owner_sub),
    -- the name exists in the default language from the start (D13)
    CHECK (full_name ? default_lang),
    -- a model card is complete in its default language before it can enter review
    CHECK (status IN ('draft', 'submitted') OR (purpose ? default_lang AND license IS NOT NULL)),
    -- verified and published models always carry the interface they were verified against
    CHECK (status NOT IN ('verified', 'published')
           OR (verified_by IS NOT NULL AND verified_interface_sha256 IS NOT NULL)),
    CHECK (jsonb_typeof(full_name)         = 'object'),
    CHECK (jsonb_typeof(short_description) = 'object'),
    CHECK (jsonb_typeof(purpose)           = 'object'),
    CHECK (jsonb_typeof(limitations_risks) = 'object'),
    CHECK (jsonb_typeof(keywords)          = 'object'),
    CHECK (jsonb_typeof(requirements)   = 'object'),
    CHECK (jsonb_typeof(contributors)   = 'array'),
    CHECK (jsonb_typeof(literature)     = 'array'),
    CHECK (jsonb_typeof(applications)   = 'array'),
    CHECK (jsonb_typeof(test_cases)     = 'array'),
    CHECK (jsonb_typeof(io_notes)       = 'object'),
    CHECK (jsonb_typeof(process_config) = 'object')
);
CREATE INDEX idx_model_owner      ON registry.model(owner_sub);
CREATE INDEX idx_model_exec_state ON registry.model(exec_state) WHERE exec_state IS NOT NULL;
CREATE INDEX idx_model_keywords   ON registry.model USING gin (keywords jsonb_path_ops);
-- Full-text search, if needed later: one expression index per platform language, e.g.
--   CREATE INDEX ON registry.model USING gin (to_tsvector('german',  purpose->>'de'));
--   CREATE INDEX ON registry.model USING gin (to_tsvector('english', purpose->>'en'));


-- -----------------------------------------------------------------------------
-- audit_log — registry actions and job executions. Append-only.
-- Role grants/revokes are NOT here: they never pass through UMP and are audited
-- by Keycloak admin events (REF-F11).
-- Retention (D11): `ump-retention` deletes rows older than 3 months inside a
-- transaction that sets `ump.audit_retention = on`; everything else is refused.
-- -----------------------------------------------------------------------------
CREATE TABLE registry.audit_log (
    id              bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    actor_sub       text,                  -- no FK: the log outlives accounts
    actor_roles     text[]      NOT NULL DEFAULT '{}',   -- role snapshot from the token
    action          text        NOT NULL,  -- model.status.change, job.execute, model.interface.drifted, …
    model_id        uuid,                  -- no FK, same reason
    job_id          text,                  -- public.jobs.id where applicable
    outcome         text        NOT NULL DEFAULT 'success'
                    CHECK (outcome IN ('success', 'denied', 'failure')),
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- before/after, reason, diff summary
    request_id      text
);
CREATE INDEX idx_audit_time  ON registry.audit_log(occurred_at);
CREATE INDEX idx_audit_model ON registry.audit_log(model_id, occurred_at) WHERE model_id IS NOT NULL;
CREATE INDEX idx_audit_actor ON registry.audit_log(actor_sub, occurred_at);

CREATE FUNCTION registry.audit_log_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('ump.audit_retention', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER trg_audit_guard BEFORE UPDATE OR DELETE ON registry.audit_log
    FOR EACH ROW EXECUTE FUNCTION registry.audit_log_guard();

-- Retention, as run by `ump-retention`:
--   BEGIN;
--   SET LOCAL ump.audit_retention = 'on';
--   DELETE FROM registry.audit_log WHERE occurred_at < now() - interval '3 months';
--   COMMIT;


-- -----------------------------------------------------------------------------
-- v_providers_config — source for ProviderConfigDbAdapter's in-memory snapshot.
-- Same shape as providers.yaml, plus `state` per process. Credentials are not
-- resolved here; the adapter decrypts them.
-- -----------------------------------------------------------------------------
CREATE VIEW registry.v_providers_config AS
SELECT ps.id                   AS provider_server_id,
       ps.name,
       ps.base_url             AS url,
       ps.ttw_job_done_seconds AS ttw_job_done,
       ps.auth_type,
       jsonb_agg(m.process_config
                 || jsonb_build_object('id',          m.remote_process_id,
                                       'state',       m.exec_state,
                                       'version',     m.version_label,
                                       'description', m.full_name->>m.default_lang)
                 ORDER BY m.remote_process_id) AS processes
  FROM registry.provider_server ps
  JOIN registry.model m ON m.provider_server_id = ps.id
 WHERE ps.is_active
   AND m.exec_state IS NOT NULL
 GROUP BY ps.id;
