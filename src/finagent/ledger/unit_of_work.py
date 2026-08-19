"""TransactionService 所依赖的跨 Repository 事务协议。"""

from types import TracebackType
from typing import Protocol, Self

from finagent.dashboard.manual_prices import ManualPriceRepository
from finagent.ledger.repository import LedgerTransactionRepository, PurchaseLotRepository
from finagent.portfolio.repository import HoldingRepository


class LedgerUnitOfWork(Protocol):
    """一次交易同时访问持仓、流水、批次和手工价格的原子边界。"""

    @property
    def holdings(self) -> HoldingRepository:
        """返回当前事务的持仓仓库。"""

        ...

    @property
    def manual_prices(self) -> ManualPriceRepository:
        """返回手工价格仓库；黄金清仓时同步删除无用报价。"""

        ...

    @property
    def transactions(self) -> LedgerTransactionRepository:
        """返回不可变交易流水仓库。"""

        ...

    @property
    def purchase_lots(self) -> PurchaseLotRepository:
        """返回 FIFO 买入批次仓库。"""

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
        """异常或未提交时回滚，然后释放 Session。"""

        ...

    async def commit(self) -> None:
        """原子提交本次交易涉及的全部表。"""

        ...


class LedgerUnitOfWorkFactory(Protocol):
    """为每次交易创建独立 Unit of Work。"""

    def __call__(self) -> LedgerUnitOfWork:
        """返回尚未进入的新工作单元。"""

        ...
