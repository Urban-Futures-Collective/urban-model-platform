"""create registry schema

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14

Model registry (REF-F12), in its own schema `registry`:
  - app_user          accounts seen by UMP (cache of token claims, PK = JWT sub)
  - provider_server   one OGC API Processes server = one providers.yaml entry
  - model             one remote process: model card, execution config,
                      review status, verified interface, drift state
  - audit_log         append-only activity log, 3-month retention
plus the view v_providers_config (source for ProviderConfigDbAdapter).

Follows reports/REF-F12-registry-schema.sql and docs/diagrams/model-registry-uml.mmd.
public.jobs is not changed: it joins via
provider_server.name || ':' || model.remote_process_id = jobs.process_id.
audit_log has no foreign keys because it must outlive models, jobs and accounts.
Projects (project, project_job) follow in a later migration.

Written manually like 0001: generated columns, CHECK constraints, the audit
guard trigger and the view are not covered by autogenerate.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "registry"

SLUG_PATTERN = "'^[a-z0-9]+(-[a-z0-9]+)*$'"
SHA256_PATTERN = "'^[0-9a-f]{64}$'"

MODEL_STATUSES = (
    "draft",
    "submitted",
    "in_review",
    "changes_requested",
    "verified",
    "published",
    "rejected",
    "deactivated",
)

EXEC_STATE = """
    CASE status
        WHEN 'published'         THEN 'published'
        WHEN 'in_review'         THEN 'in_review'
        WHEN 'verified'          THEN 'in_review'
        WHEN 'changes_requested' THEN 'deactivated'
        WHEN 'rejected'          THEN 'deactivated'
        WHEN 'deactivated'       THEN 'deactivated'
    END
"""

# column name -> expected jsonb_typeof
MODEL_JSON_TYPES = {
    "full_name": "object",
    "short_description": "object",
    "purpose": "object",
    "limitations_risks": "object",
    "keywords": "object",
    "requirements": "object",
    "contributors": "array",
    "literature": "array",
    "applications": "array",
    "test_cases": "array",
    "io_notes": "object",
    "process_config": "object",
}


def _now() -> sa.TextClause:
    return sa.text("now()")


def _json_default(kind: str) -> sa.TextClause:
    return sa.text("'[]'::jsonb" if kind == "array" else "'{}'::jsonb")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    # app_user — upserted just-in-time from the token. No password, no roles.
    op.create_table(
        "app_user",
        sa.Column("sub", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_now(),
        ),
        sa.PrimaryKeyConstraint("sub"),
        schema=SCHEMA,
    )

    # provider_server — `name` prefixes every process id and model-access role,
    # so the core treats it as immutable after creation.
    op.create_table(
        "provider_server",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        # NULL for entries imported from providers.yaml
        sa.Column("owner_sub", sa.Text(), nullable=True),
        sa.Column(
            "auth_type",
            sa.Text(),
            nullable=False,
            server_default="NoAuth",
        ),
        # encrypted by CredentialCipherPort (key-id prefixed); write-only
        sa.Column("credentials", BYTEA(), nullable=True),
        sa.Column(
            "ttw_job_done_seconds",
            sa.Numeric(10, 1),
            nullable=False,
            server_default="1500",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_provider_server_name"),
        sa.ForeignKeyConstraint(
            ["owner_sub"],
            [f"{SCHEMA}.app_user.sub"],
            name="fk_provider_server_owner",
        ),
        sa.CheckConstraint(f"name ~ {SLUG_PATTERN}", name="ck_provider_server_name"),
        sa.CheckConstraint(
            _in("auth_type", ("NoAuth", "BasicAuth", "ApiKey", "BearerToken")),
            name="ck_provider_server_auth_type",
        ),
        sa.CheckConstraint(
            "(auth_type = 'NoAuth') = (credentials IS NULL)",
            name="ck_provider_server_credentials",
        ),
        schema=SCHEMA,
    )

    # model — one remote process on one provider server. Status transitions are
    # enforced in the core (VerificationService); the database only keeps
    # invariants. exec_state is what the execution path sees (ProcessConfig.state).
    op.create_table(
        "model",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("provider_server_id", sa.Uuid(), nullable=False),
        sa.Column("remote_process_id", sa.Text(), nullable=False),
        sa.Column("owner_sub", sa.Text(), nullable=False),
        # model card; language maps ("L") must contain default_lang
        sa.Column("default_lang", sa.Text(), nullable=False, server_default="de"),
        sa.Column("full_name", JSONB(), nullable=False),
        *(
            sa.Column(name, JSONB(), nullable=False, server_default=_json_default(kind))
            for name, kind in MODEL_JSON_TYPES.items()
            if name not in ("full_name", "process_config")
        ),
        sa.Column("version_label", sa.Text(), nullable=True),
        sa.Column("repository_url", sa.Text(), nullable=True),
        sa.Column("code_ref", sa.Text(), nullable=True),
        sa.Column("license", sa.Text(), nullable=True),  # SPDX expression
        # execution config: the providers.yaml process entry
        sa.Column(
            "process_config",
            JSONB(),
            nullable=False,
            server_default=_json_default("object"),
        ),
        # lifecycle
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column(
            "exec_state",
            sa.Text(),
            sa.Computed(EXEC_STATE, persisted=True),
            nullable=True,
        ),
        sa.Column("status_note", sa.Text(), nullable=True),
        sa.Column("status_changed_by", sa.Text(), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        # verification
        sa.Column("verified_by", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_job_id", sa.Text(), nullable=True),
        sa.Column("verified_interface", JSONB(), nullable=True),
        sa.Column("verified_interface_sha256", sa.Text(), nullable=True),
        # interface drift (policy A: flag only)
        sa.Column("current_interface_sha256", sa.Text(), nullable=True),
        sa.Column(
            "interface_state",
            sa.Text(),
            nullable=False,
            server_default="unchecked",
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_model_slug"),
        sa.UniqueConstraint(
            "provider_server_id",
            "remote_process_id",
            name="uq_model_server_process",
        ),
        # no ON DELETE CASCADE: removing a server or user must not drop models
        sa.ForeignKeyConstraint(
            ["provider_server_id"],
            [f"{SCHEMA}.provider_server.id"],
            name="fk_model_provider_server",
        ),
        sa.ForeignKeyConstraint(
            ["owner_sub"], [f"{SCHEMA}.app_user.sub"], name="fk_model_owner"
        ),
        sa.ForeignKeyConstraint(
            ["status_changed_by"],
            [f"{SCHEMA}.app_user.sub"],
            name="fk_model_status_changed_by",
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"], [f"{SCHEMA}.app_user.sub"], name="fk_model_verified_by"
        ),
        sa.CheckConstraint(f"slug ~ {SLUG_PATTERN}", name="ck_model_slug"),
        sa.CheckConstraint(
            "default_lang ~ '^[a-z]{2,3}(-[A-Z]{2})?$'", name="ck_model_default_lang"
        ),
        sa.CheckConstraint(
            "license ~ '^[A-Za-z0-9.+:()\\- ]+$'", name="ck_model_license"
        ),
        sa.CheckConstraint(_in("status", MODEL_STATUSES), name="ck_model_status"),
        sa.CheckConstraint(
            _in("interface_state", ("unchecked", "in_sync", "drifted", "unreachable")),
            name="ck_model_interface_state",
        ),
        sa.CheckConstraint(
            f"verified_interface_sha256 ~ {SHA256_PATTERN}",
            name="ck_model_verified_sha256",
        ),
        sa.CheckConstraint(
            f"current_interface_sha256 ~ {SHA256_PATTERN}",
            name="ck_model_current_sha256",
        ),
        # four-eyes: the owner never verifies their own model
        sa.CheckConstraint(
            "verified_by IS NULL OR verified_by <> owner_sub",
            name="ck_model_four_eyes",
        ),
        # the name exists in the default language from the start
        sa.CheckConstraint("full_name ? default_lang", name="ck_model_full_name_lang"),
        # the card is complete in its default language before it enters review
        sa.CheckConstraint(
            "status IN ('draft', 'submitted') "
            "OR (purpose ? default_lang AND license IS NOT NULL)",
            name="ck_model_card_complete",
        ),
        # verified/published models carry the interface they were verified against
        sa.CheckConstraint(
            "status NOT IN ('verified', 'published') "
            "OR (verified_by IS NOT NULL AND verified_interface_sha256 IS NOT NULL)",
            name="ck_model_verified",
        ),
        *(
            sa.CheckConstraint(
                f"jsonb_typeof({name}) = '{kind}'", name=f"ck_model_{name}_type"
            )
            for name, kind in MODEL_JSON_TYPES.items()
        ),
        schema=SCHEMA,
    )
    op.create_index("idx_model_owner", "model", ["owner_sub"], schema=SCHEMA)
    op.create_index(
        "idx_model_exec_state",
        "model",
        ["exec_state"],
        schema=SCHEMA,
        postgresql_where=sa.text("exec_state IS NOT NULL"),
    )
    op.create_index(
        "idx_model_keywords",
        "model",
        ["keywords"],
        schema=SCHEMA,
        postgresql_using="gin",
        postgresql_ops={"keywords": "jsonb_path_ops"},
    )

    # audit_log — append-only. No foreign keys: the log outlives accounts,
    # models and jobs. Role grants/revokes are audited by Keycloak (REF-F11).
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_now(),
        ),
        sa.Column("actor_sub", sa.Text(), nullable=True),
        # role snapshot from the token
        sa.Column(
            "actor_roles",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False, server_default="success"),
        sa.Column(
            "details",
            JSONB(),
            nullable=False,
            server_default=_json_default("object"),
        ),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            _in("outcome", ("success", "denied", "failure")),
            name="ck_audit_log_outcome",
        ),
        schema=SCHEMA,
    )
    op.create_index("idx_audit_time", "audit_log", ["occurred_at"], schema=SCHEMA)
    op.create_index(
        "idx_audit_model",
        "audit_log",
        ["model_id", "occurred_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("model_id IS NOT NULL"),
    )
    op.create_index(
        "idx_audit_actor", "audit_log", ["actor_sub", "occurred_at"], schema=SCHEMA
    )

    # Retention (`ump-retention`) deletes old rows inside a transaction that
    # sets `ump.audit_retention = on`; every other UPDATE/DELETE is refused.
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.audit_log_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('ump.audit_retention', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit_log is append-only'
                USING ERRCODE = 'insufficient_privilege';
        END $$
        """)
    op.execute(f"""
        CREATE TRIGGER trg_audit_guard BEFORE UPDATE OR DELETE ON {SCHEMA}.audit_log
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.audit_log_guard()
        """)

    # Source for ProviderConfigDbAdapter: providers.yaml shape plus `state`.
    # Credentials are not resolved here; the adapter decrypts them.
    op.execute(f"""
        CREATE VIEW {SCHEMA}.v_providers_config AS
        SELECT ps.id                   AS provider_server_id,
               ps.name,
               ps.base_url             AS url,
               ps.ttw_job_done_seconds AS ttw_job_done,
               ps.auth_type,
               jsonb_agg(m.process_config
                         || jsonb_build_object(
                                'id',          m.remote_process_id,
                                'state',       m.exec_state,
                                'version',     m.version_label,
                                'description', m.full_name->>m.default_lang)
                         ORDER BY m.remote_process_id) AS processes
          FROM {SCHEMA}.provider_server ps
          JOIN {SCHEMA}.model m ON m.provider_server_id = ps.id
         WHERE ps.is_active
           AND m.exec_state IS NOT NULL
         GROUP BY ps.id
        """)


def downgrade() -> None:
    op.execute(f"DROP VIEW {SCHEMA}.v_providers_config")
    op.execute(f"DROP TRIGGER trg_audit_guard ON {SCHEMA}.audit_log")
    op.execute(f"DROP FUNCTION {SCHEMA}.audit_log_guard()")
    op.drop_index("idx_audit_actor", table_name="audit_log", schema=SCHEMA)
    op.drop_index("idx_audit_model", table_name="audit_log", schema=SCHEMA)
    op.drop_index("idx_audit_time", table_name="audit_log", schema=SCHEMA)
    op.drop_table("audit_log", schema=SCHEMA)
    op.drop_index("idx_model_keywords", table_name="model", schema=SCHEMA)
    op.drop_index("idx_model_exec_state", table_name="model", schema=SCHEMA)
    op.drop_index("idx_model_owner", table_name="model", schema=SCHEMA)
    op.drop_table("model", schema=SCHEMA)
    op.drop_table("provider_server", schema=SCHEMA)
    op.drop_table("app_user", schema=SCHEMA)
    op.execute(f"DROP SCHEMA {SCHEMA}")
