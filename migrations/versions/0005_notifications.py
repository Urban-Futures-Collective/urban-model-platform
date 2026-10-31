"""email notifications (registry phase E2)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29

The audit log is the outbox (REF-F12 "Notifications", D14): a dispatcher reads
audit_log rows after a cursor and mails the people concerned. The audit guard
forbids UPDATE, so delivery state is the single-row `notification_cursor`
rather than a flag per audit row. It starts at the current end of the log, so
existing history is never mailed.

`app_user` gains what the dispatcher needs to address people:
  - platform_roles  cache of the platform roles from the user's last token;
                    finds recipients ("all verifiers"), never authorizes
  - lang            mail language (token `locale`, if a platform language)
  - notify          per-category opt-in/out, {"<category>": true|false}
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "registry"


def upgrade() -> None:
    op.create_table(
        "notification_cursor",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        # highest audit_log.id fully processed
        sa.Column("last_audit_id", sa.BigInteger(), nullable=False),
        # event currently being retried after a transient mail error
        sa.Column("failing_audit_id", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_notification_cursor_single_row"),
        schema=SCHEMA,
    )
    op.execute(
        f"INSERT INTO {SCHEMA}.notification_cursor (id, last_audit_id) "
        f"SELECT 1, coalesce(max(id), 0) FROM {SCHEMA}.audit_log"
    )

    op.add_column(
        "app_user",
        sa.Column(
            "platform_roles",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "app_user", sa.Column("lang", sa.Text(), nullable=True), schema=SCHEMA
    )
    op.add_column(
        "app_user",
        sa.Column(
            "notify",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_app_user_notify_type",
        "app_user",
        "jsonb_typeof(notify) = 'object'",
        schema=SCHEMA,
    )
    op.create_index(
        "idx_app_user_platform_roles",
        "app_user",
        ["platform_roles"],
        schema=SCHEMA,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_app_user_platform_roles", "app_user", schema=SCHEMA)
    op.drop_constraint("ck_app_user_notify_type", "app_user", schema=SCHEMA)
    for column in ("notify", "lang", "platform_roles"):
        op.drop_column("app_user", column, schema=SCHEMA)
    op.drop_table("notification_cursor", schema=SCHEMA)
