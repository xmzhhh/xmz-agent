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


class MemoryItemNotFoundError(MemoryDomainError):
    """请求的长期记忆条目不存在。"""


class MemoryConflictError(MemoryDomainError):
    """记忆主键、ACTIVE 身份或来源关系发生冲突。"""
