"""add registry tables draft

Revision ID: 15f7acd71284
Revises: 0002
Create Date: 2026-10-14 17:42:12.775334

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = '15f7acd71284'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# text: sa.String()
# jsonb: JSONB()
# timestampt: sa.DateTime(timezone=True)
# 

def upgrade() -> None:

    op.create_table(
            'model',
            sa.Column('id', sa.Uuid(), autoincrement=True, nullable=False), # PK
            sa.Column('slug', sa.String(), unique=True, nullable=False), # single-column UK
            sa.Column('provider_server_id', sa.String(), nullable=False), # FK
            sa.Column('remote_process_id', sa.String(), nullable=False), # UK[server,process] # TODO
            sa.Column('owner_sub', sa.String(), nullable=False), # FK
            sa.Column('default_lang', sa.String(), nullable=False),
            sa.Column('full_name', JSONB(), nullable=False),
            sa.Column('short_description', JSONB(), nullable=False),
            sa.Column('purpose', JSONB(), nullable=False),
            sa.Column('limitations_risks', JSONB(), nullable=False),
            sa.Column('keywords', JSONB(), nullable=False),
            sa.Column('version_label', sa.String(), nullable=False),
            sa.Column('repository_url', sa.String(), nullable=True),
            sa.Column('code_ref', sa.String(), nullable=True),
            sa.Column('license', sa.String(), nullable=False),
            sa.Column('requirements', JSONB(), nullable=False),
            sa.Column('conributors', JSONB(), nullable=False),
            sa.Column('literature', JSONB(), nullable=True),
            sa.Column('applications', JSONB(), nullable=False),
            sa.Column('test_cases', JSONB(), nullable=True),
            sa.Column('io_notes', JSONB(), nullable=True),
            sa.Column('process_config', JSONB(), nullable=False),
            sa.Column('status', sa.String(), nullable=False),
            sa.Column('exec_state', sa.String(), nullable=True),
            sa.Column('status_note', sa.String(), nullable=True),
            sa.Column('status_changed_by', sa.String(), nullable=True), # FK
            sa.Column('status_changed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('verified_by', sa.String(), nullable=True), # FK
            sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('verification_job_id', sa.String(), nullable=True),
            sa.Column('verified_interface', JSONB(), nullable=True),
            sa.Column('verified_interface_sha256', sa.String(), nullable=True),
            sa.Column('current_interface_sha256', sa.String(), nullable=True),
            sa.Column('interface_state', sa.String(), nullable=False),
            sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['provider_server_id'], ['provider_server.id'], ondelete="CASCADE"), # "runs on" arrow in diagram
            sa.ForeignKeyConstraint(['owner_sub'], ['app_user.sub'], ondelete="CASCADE"), # "owner_sub" arrow
            sa.ForeignKeyConstraint(['verified_by'], ['app_user.sub'], ondelete="CASCADE"), # "verified_by" arrow
            sa.ForeignKeyConstraint(['status_changed_by'], ['app_user.sub'], ondelete="CASCADE"), # "status_changed_by" arrow
        )

    op.create_table(
            'app_user',
            sa.Column('sub', sa.String(), nullable=False), # PK 
            # TODO: 'sub' connects to keycloak ("sub" arrow) and to existing UMP backend 'jobs.user_id' ("user_id=sub" arrow) 
            sa.Column('display_name', sa.String(), nullable=False),
            sa.Column('email', sa.String(), nullable=True),
            sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("sub"),
        )

    op.create_table(
            'provider_server',
            sa.Column('id', sa.Uuid(), nullable=False), # PK
            sa.Column('name', sa.String(), nullable=False), # UK[process-id prefix] # TODO
            sa.Column('base_url', sa.String(), nullable=False),
            sa.Column('owner_sub', sa.String(), nullable=False), # FK
            sa.Column('auth_type', sa.String(), nullable=False), # one of the following: "NoAuth / BasicAuth / ApiKey / BearerToken"
            sa.Column('credentials', sa.String(), nullable=False) # encrypted, write-only # TODO
            sa.Column('ttw_job_done_seconds', sa.BigInteger(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('last_discovered_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(['owner_sub'], ['app_user.sub'], ondelete="CASCADE"), # "owner" arrow
        )

    op.create_table(
            'audit_log',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('occured_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('actor_sub', sa.String(), nullable=False),
            sa.Column('actor_roles', sa.String(), nullable=False),
            sa.Column('action', sa.String(), nullable=False),
            sa.Column('model_id', sa.Uuid(), nullable=True), # FK
            sa.Column('job_id', sa.String(), nullable=True), # or False? # TODO
            sa.Column('outcome', sa.String(), nullable=False), # "one of the following: success / denied / failure" # TODO
            sa.Column('details', JSONB(), nullable=True),
            sa.Column('request_id', sa.String(), nullable=False), # or nullable=True? # TODO
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['model_id'], ['model.id'], ondelete="CASCADE") # "model id" arrow (correct?)
        )   

    # # updates to the "JOBS" table:
    # # column "process_id" should contain the composite string: provider_server.name:model.remote_process_id
    # op.alter_column(table_name='jobs', column_name='process_id', ...) # TODO

def downgrade() -> None:
    """Downgrade schema."""
    pass
