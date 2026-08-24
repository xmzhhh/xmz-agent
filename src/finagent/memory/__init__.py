"""Agent 结构化记忆的领域模型与公开类型。

本包只描述会话、消息和长期记忆的合法状态，不直接操作 SQLAlchemy。数据库适配器、候选
记忆抽取和上下文组装将在后续小阶段分别实现，避免领域规则与存储或模型厂商绑定。
"""

from finagent.memory.models import (
    ConversationMessage,
    ConversationSession,
    ConversationStatus,
    MemoryActor,
    MemoryEvent,
    MemoryEventType,
    MemoryItem,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
)

__all__ = [
    "ConversationMessage",
    "ConversationSession",
    "ConversationStatus",
    "MemoryActor",
    "MemoryEvent",
    "MemoryEventType",
    "MemoryItem",
    "MemoryScopeType",
    "MemoryStatus",
    "MemoryType",
]
