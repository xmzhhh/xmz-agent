"""Agent 结构化记忆的领域模型与公开类型。

本包只描述会话、消息和长期记忆的合法状态，不直接操作 SQLAlchemy。数据库适配器、候选
记忆抽取和上下文组装将在后续小阶段分别实现，避免领域规则与存储或模型厂商绑定。
"""

from finagent.memory.commands import (
    MemoryCandidateCreate,
    MemoryConfirmationResult,
    MemoryRejectionReason,
)
from finagent.memory.errors import (
    ConversationArchivedError,
    ConversationConflictError,
    ConversationMessageNotFoundError,
    ConversationNotFoundError,
    InvalidMemoryTransitionError,
    MemoryAuditError,
    MemoryCandidateExpiredError,
    MemoryClockError,
    MemoryConflictError,
    MemoryDomainError,
    MemoryItemNotFoundError,
    MemorySourceError,
    SensitiveMemoryError,
)
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
from finagent.memory.repository import ConversationRepository, MemoryRepository
from finagent.memory.service import MemoryService
from finagent.memory.unit_of_work import MemoryUnitOfWork, MemoryUnitOfWorkFactory

__all__ = [
    "ConversationMessage",
    "ConversationArchivedError",
    "ConversationConflictError",
    "ConversationMessageNotFoundError",
    "ConversationNotFoundError",
    "ConversationRepository",
    "ConversationSession",
    "ConversationStatus",
    "InvalidMemoryTransitionError",
    "MemoryActor",
    "MemoryAuditError",
    "MemoryCandidateCreate",
    "MemoryCandidateExpiredError",
    "MemoryClockError",
    "MemoryConfirmationResult",
    "MemoryConflictError",
    "MemoryDomainError",
    "MemoryEvent",
    "MemoryEventType",
    "MemoryItem",
    "MemoryItemNotFoundError",
    "MemoryRepository",
    "MemoryRejectionReason",
    "MemoryScopeType",
    "MemoryStatus",
    "MemoryService",
    "MemorySourceError",
    "MemoryType",
    "MemoryUnitOfWork",
    "MemoryUnitOfWorkFactory",
    "SensitiveMemoryError",
]
