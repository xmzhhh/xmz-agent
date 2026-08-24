"""会话短期记忆与长期结构化记忆的异步 Repository 协议。

上层 MemoryService 只依赖这些最小接口，不知道底层使用 SQLite、SQLAlchemy 还是测试
Fake。Repository 负责可靠读写和模型转换，不负责候选抽取、敏感信息判断或冲突决策。
"""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from finagent.llm import Message
from finagent.memory.models import (
    ConversationMessage,
    ConversationSession,
    ConversationStatus,
    MemoryEvent,
    MemoryItem,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
)


class ConversationRepository(Protocol):
    """会话及其有序消息的最小数据访问接口。"""

    async def add_session(self, session: ConversationSession) -> ConversationSession:
        """创建会话；已存在相同 UUID 时拒绝覆盖。"""

        ...

    async def get_session(self, session_id: UUID) -> ConversationSession:
        """按 UUID 读取会话，不存在时抛出稳定领域异常。"""

        ...

    async def list_sessions(
        self,
        status: ConversationStatus | None = None,
    ) -> tuple[ConversationSession, ...]:
        """按最近更新时间倒序读取会话，可选按状态过滤。"""

        ...

    async def update_session(self, session: ConversationSession) -> ConversationSession:
        """更新标题、状态和滚动摘要，不允许改写创建时间。"""

        ...

    async def delete_session(self, session_id: UUID) -> ConversationSession:
        """删除会话及其短期消息，并返回删除前的会话。"""

        ...

    async def append_message(
        self,
        session_id: UUID,
        message: Message,
        *,
        created_at: datetime,
    ) -> ConversationMessage:
        """由仓库原子分配下一序号并追加一条标准模型消息。"""

        ...

    async def list_messages(
        self,
        session_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[ConversationMessage, ...]:
        """按序号读取指定会话的消息窗口。"""

        ...


class MemoryRepository(Protocol):
    """长期记忆正文与不含正文的审计事件访问接口。"""

    async def add_memory(self, memory: MemoryItem) -> MemoryItem:
        """新增候选或历史记忆，不允许覆盖同 UUID 数据。"""

        ...

    async def get_memory(self, memory_id: UUID) -> MemoryItem:
        """按 UUID 读取长期记忆。"""

        ...

    async def list_memories(
        self,
        *,
        status: MemoryStatus | None = None,
        memory_type: MemoryType | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
    ) -> tuple[MemoryItem, ...]:
        """按可组合条件查询记忆，并以创建时间和 UUID 保证确定顺序。"""

        ...

    async def update_memory(self, memory: MemoryItem) -> MemoryItem:
        """保存记忆内容或状态变化，不允许改写创建时间。"""

        ...

    async def delete_memory(self, memory_id: UUID) -> MemoryItem:
        """硬删除记忆正文；独立审计事件不会随之删除。"""

        ...

    async def add_event(self, event: MemoryEvent) -> MemoryEvent:
        """追加一条不含记忆正文的审计事件。"""

        ...

    async def list_events(self, memory_id: UUID) -> tuple[MemoryEvent, ...]:
        """按发生时间读取一条记忆的完整审计轨迹。"""

        ...
