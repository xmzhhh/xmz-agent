"""创建 Phase 8 会话、消息、结构化记忆和记忆事件表。

Revision ID: 20260824_01
Revises: 20260817_01
Create Date: 2026-08-24 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_01"
down_revision: str | Sequence[str] | None = "20260817_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按外键依赖顺序创建会话、消息、记忆和审计表。"""

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_until_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(title) BETWEEN 1 AND 120",
            name=op.f("ck_chat_sessions_title_length"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name=op.f("ck_chat_sessions_status_supported"),
        ),
        sa.CheckConstraint(
            "summary IS NULL OR length(summary) BETWEEN 1 AND 16000",
            name=op.f("ck_chat_sessions_summary_length"),
        ),
        sa.CheckConstraint(
            "summary_until_sequence >= 0",
            name=op.f("ck_chat_sessions_summary_sequence_non_negative"),
        ),
        sa.CheckConstraint(
            "(summary IS NULL AND summary_until_sequence = 0) "
            "OR (summary IS NOT NULL AND summary_until_sequence > 0)",
            name=op.f("ck_chat_sessions_summary_state_consistent"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_sessions")),
    )
    op.create_index(
        "ix_chat_sessions_updated_at",
        "chat_sessions",
        ["updated_at"],
        unique=False,
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_calls_json", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "sequence_number > 0",
            name=op.f("ck_chat_messages_sequence_positive"),
        ),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name=op.f("ck_chat_messages_role_supported"),
        ),
        sa.CheckConstraint(
            "content IS NULL OR length(content) BETWEEN 1 AND 64000",
            name=op.f("ck_chat_messages_content_length"),
        ),
        sa.CheckConstraint(
            "length(tool_calls_json) BETWEEN 2 AND 64000",
            name=op.f("ck_chat_messages_tool_calls_json_length"),
        ),
        sa.CheckConstraint(
            "tool_call_id IS NULL OR length(tool_call_id) BETWEEN 1 AND 128",
            name=op.f("ck_chat_messages_tool_call_id_length"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name=op.f("fk_chat_messages_session_id_chat_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
        sa.UniqueConstraint(
            "session_id",
            "sequence_number",
            name=op.f("uq_chat_messages_session_sequence"),
        ),
    )
    op.create_index(
        "ix_chat_messages_session_created_at",
        "chat_messages",
        ["session_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "memory_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_type", sa.String(length=16), nullable=False),
        sa.Column("memory_key", sa.String(length=64), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_session_id", sa.Uuid(), nullable=True),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "memory_type IN ('preference', 'constraint', 'watchlist', 'goal', 'feedback')",
            name=op.f("ck_memory_items_memory_type_supported"),
        ),
        sa.CheckConstraint(
            "length(memory_key) BETWEEN 1 AND 64",
            name=op.f("ck_memory_items_memory_key_length"),
        ),
        sa.CheckConstraint(
            "length(value_json) BETWEEN 2 AND 16000",
            name=op.f("ck_memory_items_value_json_length"),
        ),
        sa.CheckConstraint(
            "scope_type IN ('global', 'asset')",
            name=op.f("ck_memory_items_scope_type_supported"),
        ),
        sa.CheckConstraint(
            "(scope_type = 'global' AND scope_id = '') "
            "OR (scope_type = 'asset' AND length(scope_id) BETWEEN 1 AND 64)",
            name=op.f("ck_memory_items_scope_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'active', 'superseded', 'rejected', 'expired')",
            name=op.f("ck_memory_items_status_supported"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_memory_items_version_positive"),
        ),
        sa.CheckConstraint(
            "(status IN ('active', 'superseded', 'expired') AND confirmed_at IS NOT NULL) "
            "OR (status IN ('candidate', 'rejected') AND confirmed_at IS NULL)",
            name=op.f("ck_memory_items_confirmation_state_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["chat_messages.id"],
            name=op.f("fk_memory_items_source_message_id_chat_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_session_id"],
            ["chat_sessions.id"],
            name=op.f("fk_memory_items_source_session_id_chat_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["memory_items.id"],
            name=op.f("fk_memory_items_supersedes_id_memory_items"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    op.create_index(
        "ix_memory_items_status_expires_at",
        "memory_items",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_items_source_session",
        "memory_items",
        ["source_session_id"],
        unique=False,
    )
    op.create_index(
        "uq_memory_items_active_identity",
        "memory_items",
        ["memory_type", "memory_key", "scope_type", "scope_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "memory_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "event_type IN "
            "('candidate_created', 'confirmed', 'rejected', 'superseded', 'expired', 'deleted')",
            name=op.f("ck_memory_events_event_type_supported"),
        ),
        sa.CheckConstraint(
            "actor IN ('user', 'model', 'system')",
            name=op.f("ck_memory_events_actor_supported"),
        ),
        sa.CheckConstraint(
            "length(details_json) BETWEEN 2 AND 4000",
            name=op.f("ck_memory_events_details_json_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_events")),
    )
    op.create_index(
        "ix_memory_events_memory_occurred_at",
        "memory_events",
        ["memory_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    """按外键依赖逆序删除 Phase 8 的四张表。"""

    op.drop_index("ix_memory_events_memory_occurred_at", table_name="memory_events")
    op.drop_table("memory_events")

    op.drop_index("uq_memory_items_active_identity", table_name="memory_items")
    op.drop_index("ix_memory_items_source_session", table_name="memory_items")
    op.drop_index("ix_memory_items_status_expires_at", table_name="memory_items")
    op.drop_table("memory_items")

    op.drop_index("ix_chat_messages_session_created_at", table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index("ix_chat_sessions_updated_at", table_name="chat_sessions")
    op.drop_table("chat_sessions")
