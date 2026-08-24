"""Phase 8 会话消息与结构化记忆领域模型测试。

这些测试聚焦“什么状态可以进入 Service 和数据库”：短期消息必须保持工具调用协议顺序，长期
记忆必须满足范围、来源、确认状态和时间关系。它们防止后续模型抽取器生成看似合理、实际无法
审计的记忆对象。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from finagent.llm import MessageRole, ToolCall
from finagent.memory import (
    ConversationMessage,
    ConversationSession,
    MemoryActor,
    MemoryEvent,
    MemoryEventType,
    MemoryItem,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
)

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def test_session_summary_must_match_covered_sequence() -> None:
    """有摘要时必须标出覆盖位置；没有摘要时覆盖位置必须保持为零。"""

    session = ConversationSession(
        title="  黄金仓位讨论  ",
        summary="用户正在比较黄金和基金仓位。",
        summary_until_sequence=8,
        created_at=NOW,
        updated_at=NOW,
    )

    assert session.summary_until_sequence == 8

    with pytest.raises(ValidationError, match="summary_until_sequence"):
        ConversationSession(
            title="错误摘要",
            summary=None,
            summary_until_sequence=3,
            created_at=NOW,
            updated_at=NOW,
        )


def test_conversation_message_reuses_role_specific_tool_constraints() -> None:
    """持久化消息仍必须遵守 assistant 工具请求和 tool 结果的协议字段组合。"""

    session_id = uuid4()
    assistant = ConversationMessage(
        session_id=session_id,
        sequence_number=2,
        role=MessageRole.ASSISTANT,
        tool_calls=(
            ToolCall(id="call-1", name="get_portfolio_summary", arguments={}),
        ),
        created_at=NOW,
    )
    tool = ConversationMessage(
        session_id=session_id,
        sequence_number=3,
        role=MessageRole.TOOL,
        content='{"ok":true}',
        tool_call_id="call-1",
        created_at=NOW,
    )

    assert assistant.tool_calls[0].name == "get_portfolio_summary"
    assert tool.tool_call_id == "call-1"

    with pytest.raises(ValidationError, match="tool_call_id"):
        ConversationMessage(
            session_id=session_id,
            sequence_number=4,
            role=MessageRole.TOOL,
            content='{"ok":false}',
            created_at=NOW,
        )


def test_active_asset_memory_normalizes_scope_and_requires_confirmation() -> None:
    """资产范围代码应规范化；ACTIVE 记忆必须能证明用户何时确认。"""

    memory = MemoryItem(
        memory_type=MemoryType.CONSTRAINT,
        memory_key="max_position_percent",
        value={"percent": "30"},
        scope_type=MemoryScopeType.ASSET,
        scope_id="  jd-zs-gold  ",
        status=MemoryStatus.ACTIVE,
        confirmed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )

    assert memory.scope_id == "JD-ZS-GOLD"

    with pytest.raises(ValidationError, match="必须记录确认时间"):
        MemoryItem(
            memory_type=MemoryType.PREFERENCE,
            memory_key="risk_level",
            value={"level": "low"},
            status=MemoryStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )


def test_candidate_memory_enforces_scope_source_and_time_boundaries() -> None:
    """候选记忆不能伪造确认状态，也不能留下无法追溯的孤立来源消息。"""

    with pytest.raises(ValidationError, match="全局记忆不能填写"):
        MemoryItem(
            memory_type=MemoryType.WATCHLIST,
            memory_key="watch_symbol:017811",
            value={"symbol": "017811"},
            scope_id="017811",
            created_at=NOW,
            updated_at=NOW,
        )

    with pytest.raises(ValidationError, match="来源消息存在"):
        MemoryItem(
            memory_type=MemoryType.FEEDBACK,
            memory_key="answer_style",
            value={"style": "concise"},
            source_message_id=uuid4(),
            created_at=NOW,
            updated_at=NOW,
        )

    with pytest.raises(ValidationError, match="过期时间必须晚于"):
        MemoryItem(
            memory_type=MemoryType.GOAL,
            memory_key="temporary_goal",
            value={"description": "观察黄金一周"},
            expires_at=NOW - timedelta(seconds=1),
            created_at=NOW,
            updated_at=NOW,
        )


def test_memory_event_keeps_only_auditable_operation_metadata() -> None:
    """审计事件使用独立 UUID 引用记忆，即使正文将来硬删除也能保留操作事实。"""

    memory_id = uuid4()
    event = MemoryEvent(
        memory_id=memory_id,
        event_type=MemoryEventType.CONFIRMED,
        actor=MemoryActor.USER,
        details={"version": 1},
        occurred_at=NOW,
    )

    assert event.memory_id == memory_id
    assert event.details == {"version": 1}

    with pytest.raises(ValidationError, match="必须包含时区"):
        MemoryEvent(
            memory_id=memory_id,
            event_type=MemoryEventType.DELETED,
            actor=MemoryActor.USER,
            occurred_at=datetime(2026, 8, 24, 10, 0),
        )
