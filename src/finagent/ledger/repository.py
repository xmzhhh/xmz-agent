"""交易流水与买入批次的异步 Repository 协议。

领域服务依赖本文件中的最小接口，不知道底层是 SQLAlchemy、内存 Fake 还是未来数据库。
Repository 只读写确定的数据，不负责手续费、FIFO 和已实现收益公式。
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from finagent.ledger.models import (
    LedgerTransaction,
    LedgerTransactionCreate,
    PurchaseLot,
    PurchaseLotCreate,
)


class LedgerTransactionRepository(Protocol):
    """不可变交易流水的最小数据访问接口。"""

    async def add(self, data: LedgerTransactionCreate) -> LedgerTransaction:
        """追加一笔流水；已存在的流水不能更新。"""

        ...

    async def list_transactions(self, symbol: str | None = None) -> tuple[LedgerTransaction, ...]:
        """按发生时间返回全部流水，传入代码时只查询该资产。"""

        ...

    async def latest_occurred_at(self, symbol: str) -> datetime | None:
        """返回资产最后一笔交易时间，用于阻止乱序追加。"""

        ...


class PurchaseLotRepository(Protocol):
    """买入批次及其剩余数量的数据访问接口。"""

    async def add(self, data: PurchaseLotCreate) -> PurchaseLot:
        """增加由 opening 或 buy 流水产生的新批次。"""

        ...

    async def list_open_lots(self, symbol: str) -> tuple[PurchaseLot, ...]:
        """按取得时间返回仍有剩余数量的批次，形成 FIFO 顺序。"""

        ...

    async def update_remaining(self, lot_id: UUID, remaining: Decimal) -> PurchaseLot:
        """更新卖出后批次的剩余数量。"""

        ...
