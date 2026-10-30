"""access requests (registry phase E1)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29

Creates `registry.access_request`: the workflow record of a user asking for
access to a model and an access admin deciding (REF-F12 "Access requests", D15).
It never grants anything — the grant is the Keycloak role `{provider}:{process}`,
mapped by UMP-X before the decision is recorded here. One row per lifecycle;
the step history is in audit_log. No NOTIFY trigger: the table is not part of
the provider snapshot.

Follows reports/REF-F12-registry-schema.sql.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "registry"

ACCESS_STATUSES = ("requested", "granted", "denied", "withdrawn", "revoked")
OPEN_STATUSES = ("requested", "granted")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "access_request",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_sub", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("request_note", sa.Text(), nullable=True),  # NULL: direct grant
        sa.Column("status_note", sa.Text(), nullable=True),  # note of the last step
        sa.Column("status_changed_by", sa.Text(), nullable=True),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_sub"], [f"{SCHEMA}.app_user.sub"], name="fk_access_request_user"
        ),
        sa.ForeignKeyConstraint(
            ["model_id"], [f"{SCHEMA}.model.id"], name="fk_access_request_model"
        ),
        sa.ForeignKeyConstraint(
            ["status_changed_by"],
            [f"{SCHEMA}.app_user.sub"],
            name="fk_access_request_status_changed_by",
        ),
        sa.CheckConstraint(
            _in("status", ACCESS_STATUSES), name="ck_access_request_status"
        ),
        # nobody decides on their own request
        sa.CheckConstraint(
            "status NOT IN ('granted', 'denied') "
            "OR status_changed_by IS DISTINCT FROM user_sub",
            name="ck_access_request_not_self",
        ),
        # the other side needs the reason
        sa.CheckConstraint(
            "status NOT IN ('denied', 'revoked') OR status_note IS NOT NULL",
            name="ck_access_request_note",
        ),
        schema=SCHEMA,
    )
    # at most one open record per user and model
    op.create_index(
        "uq_access_request_open",
        "access_request",
        ["user_sub", "model_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(_in("status", OPEN_STATUSES)),
    )
    op.create_index(
        "idx_access_request_queue",
        "access_request",
        ["status", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "idx_access_request_model", "access_request", ["model_id"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_table("access_request", schema=SCHEMA)
