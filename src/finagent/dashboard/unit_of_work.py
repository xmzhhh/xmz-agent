"""资产面板跨 Repository 的事务边界。

Service 只依赖本模块定义的协议，不知道事务来自 SQLite 还是内存。一次 Unit of Work 中的
持仓仓库和手工价格仓库共享同一事务；Service 只有在完整业务动作成功后才调用 ``commit``。
"""

import asyncio
from types import TracebackType
from typing import Protocol, Self

from finagent.dashboard.manual_prices import (
    InMemoryManualPriceRepository,
    ManualPriceRepository,
)
from finagent.dashboard.models import ManualPriceRecord
from finagent.portfolio.models import Holding
from finagent.portfolio.repository import HoldingRepository, InMemoryHoldingRepository


class DashboardUnitOfWork(Protocol):
    """一次资产面板状态操作使用的 Repository 与提交接口。"""

    @property
    def holdings(self) -> HoldingRepository:
        """返回绑定到当前事务的持仓仓库。"""

        ...

    @property
    def manual_prices(self) -> ManualPriceRepository:
        """返回绑定到当前事务的手工价格仓库。"""

        ...

    async def __aenter__(self) -> Self:
        """打开事务并返回当前 Unit of Work。"""

        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """异常或未提交时回滚，然后释放资源。"""

        ...

    async def commit(self) -> None:
        """提交当前业务操作产生的全部状态变化。"""

        ...


class DashboardUnitOfWorkFactory(Protocol):
    """Service 所依赖的 Unit of Work 工厂及应用级生命周期。"""

    def __call__(self) -> DashboardUnitOfWork:
        """为一次业务操作创建全新的 Unit of Work。"""

        ...

    async def initialize(self) -> None:
        """应用启动时检查底层存储是否可用。"""

        ...

    async def close(self) -> None:
        """应用退出时释放底层存储资源。"""

        ...


class InMemoryDashboardUnitOfWork:
    """把 Phase 6 的两个内存仓库包装成统一临界区。

    进入工作单元时复制两份小型内存字典；异常或遗漏 commit 时恢复快照。该实现只用于测试
    和离线验收，数据量很小，清晰的事务语义比复制成本更重要。
    """

    def __init__(
        self,
        holding_repository: InMemoryHoldingRepository,
        manual_price_repository: InMemoryManualPriceRepository,
        lock: asyncio.Lock,
    ) -> None:
        self._holding_repository = holding_repository
        self._manual_price_repository = manual_price_repository
        self._lock = lock
        self._holding_snapshot: dict[str, Holding] | None = None
        self._manual_price_snapshot: dict[str, ManualPriceRecord] | None = None
        self._committed = False

    @property
    def holdings(self) -> HoldingRepository:
        """返回共享的内存持仓仓库。"""

        return self._holding_repository

    @property
    def manual_prices(self) -> ManualPriceRepository:
        """返回共享的内存手工价格仓库。"""

        return self._manual_price_repository

    async def __aenter__(self) -> Self:
        """获取跨仓库锁，避免两个请求交错修改内存状态。"""

        await self._lock.acquire()
        self._holding_snapshot = await self._holding_repository._snapshot_state()
        self._manual_price_snapshot = await self._manual_price_repository._snapshot_state()
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """异常或遗漏 commit 时恢复两个仓库，然后释放跨仓库锁。"""

        try:
            if exc_type is not None or not self._committed:
                if self._holding_snapshot is None or self._manual_price_snapshot is None:
                    raise RuntimeError("内存 Unit of Work 缺少回滚快照")
                await self._holding_repository._restore_state(self._holding_snapshot)
                await self._manual_price_repository._restore_state(
                    self._manual_price_snapshot
                )
        finally:
            self._holding_snapshot = None
            self._manual_price_snapshot = None
            self._lock.release()

    async def commit(self) -> None:
        """标记整笔内存修改成功，退出时不恢复快照。"""

        self._committed = True


class InMemoryDashboardUnitOfWorkFactory:
    """为测试和显式离线验收创建内存 Unit of Work。"""

    def __init__(
        self,
        holding_repository: InMemoryHoldingRepository,
        manual_price_repository: InMemoryManualPriceRepository,
    ) -> None:
        self._holding_repository = holding_repository
        self._manual_price_repository = manual_price_repository
        self._lock = asyncio.Lock()

    def __call__(self) -> InMemoryDashboardUnitOfWork:
        """创建共享同一组内存仓库和锁的工作单元。"""

        return InMemoryDashboardUnitOfWork(
            self._holding_repository,
            self._manual_price_repository,
            self._lock,
        )

    async def initialize(self) -> None:
        """内存仓库不需要启动检查。"""

    async def close(self) -> None:
        """内存仓库没有需要释放的外部资源。"""
