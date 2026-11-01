"""add player wealth snapshots

Revision ID: 5daedd7ffb18
Revises: afa0d29783e3
Create Date: 2026-11-01 21:10:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = '5daedd7ffb18'
down_revision = 'afa0d29783e3'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if 'player_wealth_snapshots' not in inspector.get_table_names():
        op.create_table('player_wealth_snapshots',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('discord_id', sa.String(length=32), nullable=False),
            sa.Column('player_name', sa.String(length=128), nullable=True),
            sa.Column('date', sa.Date(), nullable=False),
            sa.Column('cash', sa.Integer(), nullable=True),
            sa.Column('bank', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('discord_id', 'date'),
        )
        op.create_index(op.f('ix_player_wealth_snapshots_discord_id'), 'player_wealth_snapshots', ['discord_id'])
        op.create_index(op.f('ix_player_wealth_snapshots_date'), 'player_wealth_snapshots', ['date'])


def downgrade():
    op.drop_table('player_wealth_snapshots')
