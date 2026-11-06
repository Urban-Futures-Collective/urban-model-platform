"""hard deletion of provider servers: retired server names

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-05

A provider server may be hard-deleted with its models while all of them are
draft, deactivated or rejected (REF-F12 "Deleting a provider server"). Its name
must never come back: it prefixes process ids (and so job history in
public.jobs) and the model-access roles in Keycloak, which would otherwise pass
to an unrelated server registered under the same name.

  - retired_server_name   one row per deleted server's name
  - retire_server_name()  records a name; called in the deleting transaction
  - trg_provider_server_name_not_retired
                          refuses an INSERT of a retired name with
                          unique_violation (23505), which the repository
                          already maps to RegistryConflictError

Both functions take the same transaction-scoped advisory lock on the name, so
a registration that races a deletion of the same name waits for it and then
sees the retired row (READ COMMITTED: each statement in plpgsql takes a fresh
snapshot).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "registry"

# advisory-lock key shared by retire_server_name() and the insert trigger
_LOCK = "pg_advisory_xact_lock(hashtextextended('ump.server_name:' || {name}, 0))"


def upgrade() -> None:
    op.create_table(
        "retired_server_name",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("name"),
        schema=SCHEMA,
    )

    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.retire_server_name(p_name text) RETURNS void
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM {_LOCK.format(name="p_name")};
            INSERT INTO {SCHEMA}.retired_server_name (name) VALUES (p_name)
                ON CONFLICT (name) DO NOTHING;
        END $$
        """)
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.provider_server_name_not_retired() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM {_LOCK.format(name="NEW.name")};
            IF EXISTS (
                SELECT 1 FROM {SCHEMA}.retired_server_name WHERE name = NEW.name
            ) THEN
                RAISE EXCEPTION 'server name % was retired by a deletion', NEW.name
                    USING ERRCODE = 'unique_violation';
            END IF;
            RETURN NEW;
        END $$
        """)
    op.execute(f"""
        CREATE TRIGGER trg_provider_server_name_not_retired
            BEFORE INSERT ON {SCHEMA}.provider_server
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.provider_server_name_not_retired()
        """)


def downgrade() -> None:
    op.execute(
        f"DROP TRIGGER trg_provider_server_name_not_retired "
        f"ON {SCHEMA}.provider_server"
    )
    op.execute(f"DROP FUNCTION {SCHEMA}.provider_server_name_not_retired()")
    op.execute(f"DROP FUNCTION {SCHEMA}.retire_server_name(text)")
    op.drop_table("retired_server_name", schema=SCHEMA)
