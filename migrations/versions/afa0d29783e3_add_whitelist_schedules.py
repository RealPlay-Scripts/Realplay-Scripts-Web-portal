"""add whitelist schedules
Revision ID: afa0d29783e3
Revises: 
Create Date: 2026-04-13 18:49:05.179639
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision = 'afa0d29783e3'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if 'whitelist_schedules' not in inspector.get_table_names():
        op.create_table('whitelist_schedules',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('label', sa.String(length=128), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False),
            sa.Column('scheduled_at', sa.DateTime(), nullable=False),
            sa.Column('repeat_type', sa.String(length=16), nullable=True),
            sa.Column('is_executed', sa.Boolean(), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id')
        )

    cols = [c['name'] for c in inspector.get_columns('application_types')]
    if 'discord_ticket_type' in cols:
        bind.execute(text('ALTER TABLE application_types DROP COLUMN discord_ticket_type'))

    eflag_cols = [c['name'] for c in inspector.get_columns('economy_flags')]
    if 'dedup_key' not in eflag_cols:
        with op.batch_alter_table('economy_flags', schema=None) as batch_op:
            batch_op.add_column(sa.Column('dedup_key', sa.String(length=64), nullable=True))
            batch_op.create_index(batch_op.f('ix_economy_flags_dedup_key'), ['dedup_key'], unique=True)

def downgrade():
    with op.batch_alter_table('economy_flags', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_economy_flags_dedup_key'))
        batch_op.drop_column('dedup_key')
    op.drop_table('whitelist_schedules')
