"""MemoryService 所依赖的跨 Repository 事务协议。

一轮记忆操作通常会同时写消息、记忆正文和审计事件。把三者放入同一 Unit of Work，
可以保证“正文已生效但确认事件丢失”这类半完成状态不会进入数据库。
"""

from types import TracebackType
from typing import Protocol, Self

from finagent.memory.repository import ConversationRepository, MemoryRepository


class MemoryUnitOfWork(Protocol):
    """一次会话或长期记忆操作的原子事务边界。"""

    @property
    def conversations(self) -> ConversationRepository:
        """返回当前事务中的会话与消息仓库。"""

        ...

    @property
    def memories(self) -> MemoryRepository:
        """返回当前事务中的长期记忆与审计仓库。"""

        ...

    async def __aenter__(self) -> Self:
        """打开一次数据库事务。"""

        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """异常或未提交时回滚，然后释放数据库 Session。"""

        ...

    async def commit(self) -> None:
        """原子提交当前事务中的全部会话和记忆修改。"""

        ...


class MemoryUnitOfWorkFactory(Protocol):
    """为每次记忆业务操作创建独立工作单元。"""

    def __call__(self) -> MemoryUnitOfWork:
        """返回尚未进入的新工作单元。"""

        ...
