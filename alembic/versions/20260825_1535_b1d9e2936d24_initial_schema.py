"""initial schema

Revision ID: b1d9e2936d24
Revises: 
Created: 2026-08-25 15:35:52.623044+00:00

Before merging, check this migration is safe for a rolling deploy: old and new
code run side by side while containers cycle, so a column made NOT NULL or
dropped here breaks whatever is still running. Add nullable, backfill, tighten
next release.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'b1d9e2936d24'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('devices',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('app_version', sa.String(length=32), nullable=False),
    sa.Column('push_token', sa.String(length=256), nullable=True),
    sa.Column('refresh_token_hash', sa.String(length=128), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_devices')),
    sa.UniqueConstraint('user_id', 'push_token', name='devices_user_push_token')
    )
    op.create_index(op.f('ix_devices_user_id'), 'devices', ['user_id'], unique=False)
    op.create_table('otp_codes',
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('code_hash', sa.String(length=128), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_otp_codes'))
    )
    op.create_index('ix_otp_codes_expires', 'otp_codes', ['expires_at'], unique=False)
    op.create_index(op.f('ix_otp_codes_phone'), 'otp_codes', ['phone'], unique=False)
    op.create_index('ix_otp_codes_phone_created', 'otp_codes', ['phone', 'created_at'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=True),
    sa.Column('token_hash', sa.String(length=128), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refresh_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_refresh_tokens_token_hash'))
    )
    op.create_index(op.f('ix_refresh_tokens_device_id'), 'refresh_tokens', ['device_id'], unique=False)
    op.create_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens', ['user_id'], unique=False)
    op.create_index('ix_refresh_tokens_user_revoked', 'refresh_tokens', ['user_id', 'revoked_at'], unique=False)
    op.create_table('trial_grants',
    sa.Column('identity_hash', sa.String(length=64), nullable=False),
    sa.Column('identity_kind', sa.String(length=16), nullable=False),
    sa.Column('granted_to_user_id', sa.Uuid(), nullable=True),
    sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('device_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trial_grants'))
    )
    op.create_index('ix_trial_grants_device', 'trial_grants', ['device_id'], unique=False)
    op.create_index(op.f('ix_trial_grants_identity_hash'), 'trial_grants', ['identity_hash'], unique=True)
    op.create_table('users',
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('full_name', sa.String(length=120), nullable=False),
    sa.Column('institution', sa.String(length=160), nullable=False),
    sa.Column('program', sa.String(length=160), nullable=False),
    sa.Column('year_of_study', sa.Integer(), nullable=True),
    sa.Column('semester', sa.Integer(), nullable=True),
    sa.Column('avatar_path', sa.String(length=512), nullable=True),
    sa.Column('active_device_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_index(op.f('ix_users_deleted_at'), 'users', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_phone'), 'users', ['phone'], unique=True)
    op.create_index('ix_users_updated_at', 'users', ['updated_at'], unique=False)
    op.create_table('payments',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('reference', sa.String(length=120), nullable=False),
    sa.Column('tier', sa.String(length=16), nullable=False),
    sa.Column('amount_kes', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('channel', sa.String(length=32), nullable=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_payments_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments')),
    sa.UniqueConstraint('reference', name=op.f('uq_payments_reference'))
    )
    op.create_index('ix_payments_user_created', 'payments', ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_payments_user_id'), 'payments', ['user_id'], unique=False)
    op.create_table('plan_groups',
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('tier', sa.String(length=16), nullable=False),
    sa.Column('seats', sa.Integer(), nullable=False),
    sa.Column('invite_code', sa.String(length=12), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_plan_groups_owner_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plan_groups'))
    )
    op.create_index(op.f('ix_plan_groups_invite_code'), 'plan_groups', ['invite_code'], unique=True)
    op.create_index(op.f('ix_plan_groups_owner_id'), 'plan_groups', ['owner_id'], unique=False)
    op.create_table('study_days',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_study_days_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_study_days')),
    sa.UniqueConstraint('user_id', 'day', name='study_days_user_day')
    )
    op.create_index('ix_study_days_user_day', 'study_days', ['user_id', 'day'], unique=False)
    op.create_index(op.f('ix_study_days_user_id'), 'study_days', ['user_id'], unique=False)
    op.create_table('units',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('lecturer', sa.String(length=160), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_units_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_units')),
    sa.UniqueConstraint('user_id', 'code', name='units_user_code')
    )
    op.create_index(op.f('ix_units_deleted_at'), 'units', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_units_user_id'), 'units', ['user_id'], unique=False)
    op.create_index('ix_units_user_updated', 'units', ['user_id', 'updated_at'], unique=False)
    op.create_table('usage_counters',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('metric', sa.String(length=24), nullable=False),
    sa.Column('period_key', sa.String(length=16), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('period_date', sa.Date(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_usage_counters_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_usage_counters')),
    sa.UniqueConstraint('user_id', 'metric', 'period_key', name='usage_counters_user_metric_period')
    )
    op.create_index(op.f('ix_usage_counters_user_id'), 'usage_counters', ['user_id'], unique=False)
    op.create_table('user_settings',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('deadline_reminders', sa.Boolean(), nullable=False),
    sa.Column('class_reminders', sa.Boolean(), nullable=False),
    sa.Column('reminder_lead_minutes', sa.Integer(), nullable=False),
    sa.Column('quiet_hours_start', sa.String(length=5), nullable=False),
    sa.Column('quiet_hours_end', sa.String(length=5), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('biometric_lock', sa.Boolean(), nullable=False),
    sa.Column('biometric_kind', sa.String(length=16), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_settings_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_settings'))
    )
    op.create_index(op.f('ix_user_settings_user_id'), 'user_settings', ['user_id'], unique=True)
    op.create_table('chats',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('unit_id', sa.Uuid(), nullable=True),
    sa.Column('title', sa.String(length=120), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['unit_id'], ['units.id'], name=op.f('fk_chats_unit_id_units'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_chats_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chats'))
    )
    op.create_index(op.f('ix_chats_deleted_at'), 'chats', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_chats_unit_id'), 'chats', ['unit_id'], unique=False)
    op.create_index(op.f('ix_chats_user_id'), 'chats', ['user_id'], unique=False)
    op.create_index('ix_chats_user_updated', 'chats', ['user_id', 'updated_at'], unique=False)
    op.create_table('class_sessions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('unit_id', sa.Uuid(), nullable=False),
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.Column('starts_at', sa.Time(), nullable=False),
    sa.Column('ends_at', sa.Time(), nullable=False),
    sa.Column('room', sa.String(length=80), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['unit_id'], ['units.id'], name=op.f('fk_class_sessions_unit_id_units'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_class_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_class_sessions'))
    )
    op.create_index(op.f('ix_class_sessions_deleted_at'), 'class_sessions', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_class_sessions_unit_id'), 'class_sessions', ['unit_id'], unique=False)
    op.create_index(op.f('ix_class_sessions_user_id'), 'class_sessions', ['user_id'], unique=False)
    op.create_index('ix_class_sessions_user_weekday', 'class_sessions', ['user_id', 'weekday'], unique=False)
    op.create_table('events',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('unit_id', sa.Uuid(), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('label', sa.String(length=80), nullable=False),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('done', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['unit_id'], ['units.id'], name=op.f('fk_events_unit_id_units'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_events_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events'))
    )
    op.create_index(op.f('ix_events_deleted_at'), 'events', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_events_unit_id'), 'events', ['unit_id'], unique=False)
    op.create_index('ix_events_user_due', 'events', ['user_id', 'done', 'due_at'], unique=False)
    op.create_index(op.f('ix_events_user_id'), 'events', ['user_id'], unique=False)
    op.create_index('ix_events_user_updated', 'events', ['user_id', 'updated_at'], unique=False)
    op.create_table('materials',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('unit_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('storage_bucket', sa.String(length=32), nullable=True),
    sa.Column('storage_path', sa.String(length=512), nullable=True),
    sa.Column('byte_size', sa.BigInteger(), nullable=True),
    sa.Column('mime_type', sa.String(length=128), nullable=True),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('extraction_status', sa.String(length=16), nullable=False),
    sa.Column('extraction_error', sa.Text(), nullable=True),
    sa.Column('archived', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['unit_id'], ['units.id'], name=op.f('fk_materials_unit_id_units'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_materials_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_materials'))
    )
    op.create_index(op.f('ix_materials_deleted_at'), 'materials', ['deleted_at'], unique=False)
    op.create_index('ix_materials_extraction', 'materials', ['extraction_status', 'created_at'], unique=False)
    op.create_index('ix_materials_unit_archived', 'materials', ['unit_id', 'archived'], unique=False)
    op.create_index(op.f('ix_materials_unit_id'), 'materials', ['unit_id'], unique=False)
    op.create_index(op.f('ix_materials_user_id'), 'materials', ['user_id'], unique=False)
    op.create_index('ix_materials_user_updated', 'materials', ['user_id', 'updated_at'], unique=False)
    op.create_table('plan_group_members',
    sa.Column('group_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['plan_groups.id'], name=op.f('fk_plan_group_members_group_id_plan_groups'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_plan_group_members_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plan_group_members')),
    sa.UniqueConstraint('group_id', 'user_id', name='plan_group_members_group_user')
    )
    op.create_index(op.f('ix_plan_group_members_group_id'), 'plan_group_members', ['group_id'], unique=False)
    op.create_index(op.f('ix_plan_group_members_user_id'), 'plan_group_members', ['user_id'], unique=False)
    op.create_table('subscriptions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('tier', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('verified', sa.Boolean(), nullable=False),
    sa.Column('group_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['plan_groups.id'], name=op.f('fk_subscriptions_group_id_plan_groups'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_subscriptions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_subscriptions'))
    )
    op.create_index('ix_subscriptions_expires', 'subscriptions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_subscriptions_group_id'), 'subscriptions', ['group_id'], unique=False)
    op.create_index(op.f('ix_subscriptions_user_id'), 'subscriptions', ['user_id'], unique=True)
    op.create_table('material_chunks',
    sa.Column('material_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('page_number', sa.Integer(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], name=op.f('fk_material_chunks_material_id_materials'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_material_chunks'))
    )
    op.create_index(op.f('ix_material_chunks_material_id'), 'material_chunks', ['material_id'], unique=False)
    op.create_index('ix_material_chunks_material_ordinal', 'material_chunks', ['material_id', 'ordinal'], unique=False)
    op.create_index(op.f('ix_material_chunks_user_id'), 'material_chunks', ['user_id'], unique=False)
    op.create_table('messages',
    sa.Column('chat_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('sources', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('completion_tokens', sa.Integer(), nullable=True),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chat_id'], ['chats.id'], name=op.f('fk_messages_chat_id_chats'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages'))
    )
    op.create_index('ix_messages_chat_created', 'messages', ['chat_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_messages_chat_id'), 'messages', ['chat_id'], unique=False)
    op.create_index(op.f('ix_messages_user_id'), 'messages', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_messages_chat_created', table_name='messages')
    op.drop_index(op.f('ix_messages_chat_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_user_id'), table_name='messages')
    op.drop_table('messages')
    op.drop_index(op.f('ix_material_chunks_material_id'), table_name='material_chunks')
    op.drop_index('ix_material_chunks_material_ordinal', table_name='material_chunks')
    op.drop_index(op.f('ix_material_chunks_user_id'), table_name='material_chunks')
    op.drop_table('material_chunks')
    op.drop_index('ix_subscriptions_expires', table_name='subscriptions')
    op.drop_index(op.f('ix_subscriptions_group_id'), table_name='subscriptions')
    op.drop_index(op.f('ix_subscriptions_user_id'), table_name='subscriptions')
    op.drop_table('subscriptions')
    op.drop_index(op.f('ix_plan_group_members_group_id'), table_name='plan_group_members')
    op.drop_index(op.f('ix_plan_group_members_user_id'), table_name='plan_group_members')
    op.drop_table('plan_group_members')
    op.drop_index(op.f('ix_materials_deleted_at'), table_name='materials')
    op.drop_index('ix_materials_extraction', table_name='materials')
    op.drop_index('ix_materials_unit_archived', table_name='materials')
    op.drop_index(op.f('ix_materials_unit_id'), table_name='materials')
    op.drop_index(op.f('ix_materials_user_id'), table_name='materials')
    op.drop_index('ix_materials_user_updated', table_name='materials')
    op.drop_table('materials')
    op.drop_index(op.f('ix_events_deleted_at'), table_name='events')
    op.drop_index(op.f('ix_events_unit_id'), table_name='events')
    op.drop_index('ix_events_user_due', table_name='events')
    op.drop_index(op.f('ix_events_user_id'), table_name='events')
    op.drop_index('ix_events_user_updated', table_name='events')
    op.drop_table('events')
    op.drop_index(op.f('ix_class_sessions_deleted_at'), table_name='class_sessions')
    op.drop_index(op.f('ix_class_sessions_unit_id'), table_name='class_sessions')
    op.drop_index(op.f('ix_class_sessions_user_id'), table_name='class_sessions')
    op.drop_index('ix_class_sessions_user_weekday', table_name='class_sessions')
    op.drop_table('class_sessions')
    op.drop_index(op.f('ix_chats_deleted_at'), table_name='chats')
    op.drop_index(op.f('ix_chats_unit_id'), table_name='chats')
    op.drop_index(op.f('ix_chats_user_id'), table_name='chats')
    op.drop_index('ix_chats_user_updated', table_name='chats')
    op.drop_table('chats')
    op.drop_index(op.f('ix_user_settings_user_id'), table_name='user_settings')
    op.drop_table('user_settings')
    op.drop_index(op.f('ix_usage_counters_user_id'), table_name='usage_counters')
    op.drop_table('usage_counters')
    op.drop_index(op.f('ix_units_deleted_at'), table_name='units')
    op.drop_index(op.f('ix_units_user_id'), table_name='units')
    op.drop_index('ix_units_user_updated', table_name='units')
    op.drop_table('units')
    op.drop_index('ix_study_days_user_day', table_name='study_days')
    op.drop_index(op.f('ix_study_days_user_id'), table_name='study_days')
    op.drop_table('study_days')
    op.drop_index(op.f('ix_plan_groups_invite_code'), table_name='plan_groups')
    op.drop_index(op.f('ix_plan_groups_owner_id'), table_name='plan_groups')
    op.drop_table('plan_groups')
    op.drop_index('ix_payments_user_created', table_name='payments')
    op.drop_index(op.f('ix_payments_user_id'), table_name='payments')
    op.drop_table('payments')
    op.drop_index(op.f('ix_users_deleted_at'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_index(op.f('ix_users_phone'), table_name='users')
    op.drop_index('ix_users_updated_at', table_name='users')
    op.drop_table('users')
    op.drop_index('ix_trial_grants_device', table_name='trial_grants')
    op.drop_index(op.f('ix_trial_grants_identity_hash'), table_name='trial_grants')
    op.drop_table('trial_grants')
    op.drop_index(op.f('ix_refresh_tokens_device_id'), table_name='refresh_tokens')
    op.drop_index(op.f('ix_refresh_tokens_user_id'), table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_user_revoked', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index('ix_otp_codes_expires', table_name='otp_codes')
    op.drop_index(op.f('ix_otp_codes_phone'), table_name='otp_codes')
    op.drop_index('ix_otp_codes_phone_created', table_name='otp_codes')
    op.drop_table('otp_codes')
    op.drop_index(op.f('ix_devices_user_id'), table_name='devices')
    op.drop_table('devices')
