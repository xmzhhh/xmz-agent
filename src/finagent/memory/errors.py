"""Agent 记忆领域对应用层暴露的稳定异常。

Repository 会把数据库主键、唯一索引和外键错误转换为这里的异常，避免后续
MemoryService、CLI 或 Web API 依赖 SQLAlchemy 的具体异常类型。
"""


class MemoryDomainError(RuntimeError):
    """所有会话记忆与长期记忆业务异常的基类。"""


class ConversationNotFoundError(MemoryDomainError):
    """请求的会话不存在。"""


class ConversationConflictError(MemoryDomainError):
    """会话主键、消息序号或不可变字段发生冲突。"""


class ConversationArchivedError(MemoryDomainError):
    """归档会话不允许继续追加消息。"""


class ConversationMessageNotFoundError(MemoryDomainError):
    """请求的会话消息不存在。"""


class MemoryItemNotFoundError(MemoryDomainError):
    """请求的长期记忆条目不存在。"""


class MemoryConflictError(MemoryDomainError):
    """记忆主键、ACTIVE 身份或来源关系发生冲突。"""


class InvalidMemoryTransitionError(MemoryDomainError):
    """当前记忆状态不允许执行请求的确认或拒绝操作。"""


class MemoryCandidateExpiredError(MemoryDomainError):
    """候选记忆已经到达有效期，不能再被确认。"""


class SensitiveMemoryError(MemoryDomainError):
    """候选内容包含禁止持久化的凭据或身份字段。"""


class MemorySourceError(MemoryDomainError):
    """候选记忆的来源消息不存在、不匹配或不是用户消息。"""


class MemoryClockError(MemoryDomainError):
    """服务端时钟缺少时区或发生时间倒退。"""


class MemoryAuditError(MemoryDomainError):
    """代码尝试把未列入白名单的字段写入记忆审计事件。"""


class ConversationHistoryError(MemoryDomainError):
    """持久化消息顺序无法组成合法 user/assistant/tool 对话轮次。"""


class ConversationSummaryError(MemoryDomainError):
    """摘要器返回空内容、工具调用或过长结果。"""


class ConversationSummaryConflictError(MemoryDomainError):
    """摘要生成期间已有另一操作推进了摘要覆盖位置。"""
