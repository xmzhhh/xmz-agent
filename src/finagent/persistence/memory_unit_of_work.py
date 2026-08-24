"""基于 SQLAlchemy AsyncSession 的 Agent 记忆 Unit of Work。

会话消息、长期记忆和审计事件共享同一数据库事务；资产面板继续使用自己的 Unit of Work。
二者复用 :class:`DatabaseManager`，但不把不相关的 Repository 暴露给彼此的业务服务。
"""

import asyncio
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from finagent.memory.repository import ConversationRepository, MemoryRepository
from finagent.persistence.database import DatabaseManager
from finagent.persistence.memory_repositories import (
    SqlAlchemyConversationRepository,
    SqlAlchemyMemoryRepository,
)
from finagent.persistence.unit_of_work import CURRENT_SCHEMA_REVISION


class SqlAlchemyMemoryUnitOfWork:
    """让会话与记忆 Repository 共享一次提交或回滚。"""

    def __init__(
        self,
        database_manager: DatabaseManager,
        lock: asyncio.Lock,
    ) -> None:
        self._database_manager = database_manager
        self._lock = lock
        self._session: AsyncSession | None = None
        self._conversations: SqlAlchemyConversationRepository | None = None
        self._memories: SqlAlchemyMemoryRepository | None = None
        self._committed = False

    @property
    def conversations(self) -> ConversationRepository:
        """返回绑定到当前 Session 的会话与消息仓库。"""

        if self._conversations is None:
            raise RuntimeError("Memory Unit of Work 尚未进入，不能访问会话仓库")
        return self._conversations

    @property
    def memories(self) -> MemoryRepository:
        """返回绑定到当前 Session 的长期记忆与审计仓库。"""

        if self._memories is None:
            raise RuntimeError("Memory Unit of Work 尚未进入，不能访问记忆仓库")
        return self._memories

    async def __aenter__(self) -> Self:
        """串行进入本地 SQLite 写事务，并创建共享 Session 的两个 Repository。"""

        await self._lock.acquire()
        try:
            self._session = self._database_manager.create_session()
            self._conversations = SqlAlchemyConversationRepository(self._session)
            self._memories = SqlAlchemyMemoryRepository(self._session)
            self._committed = False
            return self
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """发生异常或遗漏 commit 时回滚，最后关闭 Session 并释放串行锁。"""

        session = self._require_session()
        try:
            if exc_type is not None or not self._committed:
                await session.rollback()
        finally:
            try:
                await session.close()
            finally:
                self._session = None
                self._conversations = None
                self._memories = None
                self._lock.release()

    async def commit(self) -> None:
        """一次提交当前 Session 内全部会话、记忆和事件修改。"""

        session = self._require_session()
        await session.commit()
        self._committed = True

    def _require_session(self) -> AsyncSession:
        """确保操作发生在 ``async with`` 管理的有效生命周期内。"""

        if self._session is None:
            raise RuntimeError("Memory Unit of Work 尚未进入或已经退出")
        return self._session


class SqlAlchemyMemoryUnitOfWorkFactory:
    """共享 DatabaseManager，并为每轮记忆操作创建独立工作单元。"""

    def __init__(self, database_manager: DatabaseManager) -> None:
        self._database_manager = database_manager
        # 仓库通过查询当前最大序号后追加消息；进程内串行事务可避免两个请求分配同一序号。
        # 数据库唯一约束仍作为跨进程或绕过应用层写入时的最后防线。
        self._lock = asyncio.Lock()

    def __call__(self) -> SqlAlchemyMemoryUnitOfWork:
        """创建尚未进入的新 Memory Unit of Work。"""

        return SqlAlchemyMemoryUnitOfWork(self._database_manager, self._lock)

    async def initialize(self) -> None:
        """应用启动时确认连接与 Alembic schema revision。"""

        await self._database_manager.check_schema(CURRENT_SCHEMA_REVISION)

    async def close(self) -> None:
        """应用退出时释放数据库连接池和 SQLite 文件句柄。"""

        await self._database_manager.close()
