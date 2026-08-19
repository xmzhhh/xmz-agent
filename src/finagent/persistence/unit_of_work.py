"""基于 SQLAlchemy AsyncSession 的资产面板 Unit of Work。"""

import asyncio
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from finagent.dashboard.manual_prices import ManualPriceRepository
from finagent.ledger.repository import LedgerTransactionRepository, PurchaseLotRepository
from finagent.persistence.database import DatabaseManager
from finagent.persistence.ledger_repositories import (
    SqlAlchemyLedgerTransactionRepository,
    SqlAlchemyPurchaseLotRepository,
)
from finagent.persistence.repositories import (
    SqlAlchemyHoldingRepository,
    SqlAlchemyManualPriceRepository,
)
from finagent.portfolio.catalog import DEFAULT_ASSET_CATALOG, AssetCatalog
from finagent.portfolio.repository import HoldingRepository

CURRENT_SCHEMA_REVISION = "20260817_01"


class SqlAlchemyDashboardUnitOfWork:
    """让持仓、价格、流水和批次 Repository 共享同一事务边界。"""

    def __init__(
        self,
        database_manager: DatabaseManager,
        catalog: AssetCatalog,
        lock: asyncio.Lock,
    ) -> None:
        self._database_manager = database_manager
        self._catalog = catalog
        self._lock = lock
        self._session: AsyncSession | None = None
        self._holdings: SqlAlchemyHoldingRepository | None = None
        self._manual_prices: SqlAlchemyManualPriceRepository | None = None
        self._transactions: SqlAlchemyLedgerTransactionRepository | None = None
        self._purchase_lots: SqlAlchemyPurchaseLotRepository | None = None
        self._committed = False

    @property
    def holdings(self) -> HoldingRepository:
        """返回绑定到当前 Session 的持仓仓库。"""

        if self._holdings is None:
            raise RuntimeError("Unit of Work 尚未进入，不能访问持仓仓库")
        return self._holdings

    @property
    def manual_prices(self) -> ManualPriceRepository:
        """返回绑定到当前 Session 的手工价格仓库。"""

        if self._manual_prices is None:
            raise RuntimeError("Unit of Work 尚未进入，不能访问手工价格仓库")
        return self._manual_prices

    @property
    def transactions(self) -> LedgerTransactionRepository:
        """返回绑定到当前 Session 的交易流水仓库。"""

        if self._transactions is None:
            raise RuntimeError("Unit of Work 尚未进入，不能访问交易流水仓库")
        return self._transactions

    @property
    def purchase_lots(self) -> PurchaseLotRepository:
        """返回绑定到当前 Session 的买入批次仓库。"""

        if self._purchase_lots is None:
            raise RuntimeError("Unit of Work 尚未进入，不能访问买入批次仓库")
        return self._purchase_lots

    async def __aenter__(self) -> Self:
        """串行进入本机状态事务，并为全部 Repository 创建同一 Session。"""

        await self._lock.acquire()
        try:
            self._session = self._database_manager.create_session()
            self._holdings = SqlAlchemyHoldingRepository(self._session, self._catalog)
            self._manual_prices = SqlAlchemyManualPriceRepository(self._session)
            self._transactions = SqlAlchemyLedgerTransactionRepository(self._session)
            self._purchase_lots = SqlAlchemyPurchaseLotRepository(self._session)
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
        """异常或遗漏 commit 时回滚，最后关闭 Session 并释放串行锁。"""

        session = self._require_session()
        try:
            if exc_type is not None or not self._committed:
                await session.rollback()
        finally:
            try:
                await session.close()
            finally:
                self._session = None
                self._holdings = None
                self._manual_prices = None
                self._transactions = None
                self._purchase_lots = None
                self._lock.release()

    async def commit(self) -> None:
        """一次提交当前 Session 内所有 Repository 的修改。"""

        session = self._require_session()
        await session.commit()
        self._committed = True

    def _require_session(self) -> AsyncSession:
        """确保调用发生在 ``async with`` 管理的有效生命周期内。"""

        if self._session is None:
            raise RuntimeError("Unit of Work 尚未进入或已经退出")
        return self._session


class SqlAlchemyDashboardUnitOfWorkFactory:
    """共享 DatabaseManager，并为每次操作创建独立工作单元。"""

    def __init__(
        self,
        database_manager: DatabaseManager,
        catalog: AssetCatalog = DEFAULT_ASSET_CATALOG,
    ) -> None:
        self._database_manager = database_manager
        self._catalog = catalog
        # 当前应用是单用户本地面板。进程内串行状态事务能避免两个写请求争用 SQLite；数据库
        # 约束仍负责防御其他进程或绕过应用的并发写入。
        self._lock = asyncio.Lock()

    def __call__(self) -> SqlAlchemyDashboardUnitOfWork:
        """创建尚未进入的新 Unit of Work。"""

        return SqlAlchemyDashboardUnitOfWork(
            self._database_manager,
            self._catalog,
            self._lock,
        )

    async def initialize(self) -> None:
        """应用启动时确认连接与 Alembic schema revision。"""

        await self._database_manager.check_schema(CURRENT_SCHEMA_REVISION)

    async def close(self) -> None:
        """应用退出时释放数据库连接池和文件句柄。"""

        await self._database_manager.close()
