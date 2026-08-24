"""Phase 8 会话、消息和结构化记忆的 SQLAlchemy ORM 模型。

这些表只负责可靠存储，不负责让模型决定哪些信息值得记忆。候选抽取、用户确认、冲突替换、
TTL 和敏感信息过滤属于后续 MemoryService；Repository 将在 ORM Row 与 memory 领域模型之间
转换。持仓与行情继续保存在各自事实表中，不进入本模块。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from finagent.persistence.database import Base
from finagent.persistence.models import UTCDateTime, _utc_now


class ChatSessionRow(Base):
    """一段独立对话及其滚动摘要位置。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        CheckConstraint("length(title) BETWEEN 1 AND 120", name="title_length"),
        CheckConstraint("status IN ('active', 'archived')", name="status_supported"),
        CheckConstraint(
            "summary IS NULL OR length(summary) BETWEEN 1 AND 16000",
            name="summary_length",
        ),
        CheckConstraint(
            "summary_until_sequence >= 0",
            name="summary_sequence_non_negative",
        ),
        CheckConstraint(
            "(summary IS NULL AND summary_until_sequence = 0) "
            "OR (summary IS NOT NULL AND summary_until_sequence > 0)",
            name="summary_state_consistent",
        ),
        Index("ix_chat_sessions_updated_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    summary_until_sequence: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )


class ChatMessageRow(Base):
    """一条可重放的模型消息，包含工具请求或对应工具结果的关联字段。"""

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("sequence_number > 0", name="sequence_positive"),
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="role_supported",
        ),
        CheckConstraint(
            "content IS NULL OR length(content) BETWEEN 1 AND 64000",
            name="content_length",
        ),
        CheckConstraint(
            "length(tool_calls_json) BETWEEN 2 AND 64000",
            name="tool_calls_json_length",
        ),
        CheckConstraint(
            "tool_call_id IS NULL OR length(tool_call_id) BETWEEN 1 AND 128",
            name="tool_call_id_length",
        ),
        UniqueConstraint(
            "session_id",
            "sequence_number",
            name="uq_chat_messages_session_sequence",
        ),
        Index("ix_chat_messages_session_created_at", "session_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text(), nullable=True)
    # 使用 JSON 文本保留 OpenAI-compatible 工具调用原始结构；Repository 负责稳定序列化。
    tool_calls_json: Mapped[str] = mapped_column(Text(), nullable=False, default="[]")
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
    )


class MemoryItemRow(Base):
    """候选或已确认的长期记忆及其版本关系。"""

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(
            "memory_type IN ('preference', 'constraint', 'watchlist', 'goal', 'feedback')",
            name="memory_type_supported",
        ),
        CheckConstraint("length(memory_key) BETWEEN 1 AND 64", name="memory_key_length"),
        CheckConstraint("length(value_json) BETWEEN 2 AND 16000", name="value_json_length"),
        CheckConstraint("scope_type IN ('global', 'asset')", name="scope_type_supported"),
        # 全局范围把 scope_id 统一保存为空串，使部分唯一索引不会受到 SQL NULL 语义影响。
        CheckConstraint(
            "(scope_type = 'global' AND scope_id = '') "
            "OR (scope_type = 'asset' AND length(scope_id) BETWEEN 1 AND 64)",
            name="scope_consistent",
        ),
        CheckConstraint(
            "status IN ('candidate', 'active', 'superseded', 'rejected', 'expired')",
            name="status_supported",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(status IN ('active', 'superseded', 'expired') AND confirmed_at IS NOT NULL) "
            "OR (status IN ('candidate', 'rejected') AND confirmed_at IS NULL)",
            name="confirmation_state_consistent",
        ),
        Index("ix_memory_items_status_expires_at", "status", "expires_at"),
        Index("ix_memory_items_source_session", "source_session_id"),
        Index(
            "uq_memory_items_active_identity",
            "memory_type",
            "memory_key",
            "scope_type",
            "scope_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    memory_type: Mapped[str] = mapped_column(String(16), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value_json: Mapped[str] = mapped_column(Text(), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_session_id: Mapped[UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("chat_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("memory_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )


class MemoryEventRow(Base):
    """不依赖记忆正文生命周期的审计事件。"""

    __tablename__ = "memory_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN "
            "('candidate_created', 'confirmed', 'rejected', 'superseded', 'expired', 'deleted')",
            name="event_type_supported",
        ),
        CheckConstraint("actor IN ('user', 'model', 'system')", name="actor_supported"),
        CheckConstraint("length(details_json) BETWEEN 2 AND 4000", name="details_json_length"),
        Index("ix_memory_events_memory_occurred_at", "memory_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    # 故意不设外键：硬删除记忆正文后仍保留其 UUID 和删除事件，但不保留 value_json。
    memory_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    details_json: Mapped[str] = mapped_column(Text(), nullable=False, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
    )
