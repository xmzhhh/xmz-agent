"""交易流水与买入批次的 SQLAlchemy Repository 实现。

两个 Repository 绑定外部传入的 AsyncSession，只执行查询、flush 和模型转换，不自行提交。
TransactionService 通过 Unit of Work 保证流水、批次和当前持仓一起成功或一起回滚。
"""

from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finagent.ledger.models import (
    LedgerTransaction,
    LedgerTransactionCreate,
    PurchaseLot,
    PurchaseLotCreate,
    TransactionType,
)
from finagent.persistence.errors import PersistenceError
from finagent.persistence.models import LedgerTransactionRow, PurchaseLotRow
from finagent.portfolio.catalog import normalize_asset_symbol
from finagent.portfolio.models import Currency


class SqlAlchemyLedgerTransactionRepository:
    """在当前 AsyncSession 中追加和查询不可变交易流水。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, data: LedgerTransactionCreate) -> LedgerTransaction:
        """追加一笔由 Service 完成计算的交易流水。"""

        row = LedgerTransactionRow(
            id=data.id,
            symbol=data.symbol,
            transaction_type=data.transaction_type.value,
            quantity=data.quantity,
            unit_price=data.unit_price,
            gross_amount=data.gross_amount,
            fee_amount=data.fee_amount,
            cash_amount=data.cash_amount,
            realized_pnl=data.realized_pnl,
            currency=data.currency.value,
            occurred_at=data.occurred_at,
            created_at=data.created_at,
            note=data.note,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise PersistenceError(f"交易流水主键冲突：{data.id}") from error
        return self._row_to_domain(row)

    async def list_transactions(
        self,
        symbol: str | None = None,
    ) -> tuple[LedgerTransaction, ...]:
        """按发生时间、记录时间和 UUID 返回确定性顺序的交易流水。"""

        statement = select(LedgerTransactionRow)
        if symbol is not None:
            statement = statement.where(
                LedgerTransactionRow.symbol == normalize_asset_symbol(symbol)
            )
        statement = statement.order_by(
            LedgerTransactionRow.occurred_at,
            LedgerTransactionRow.created_at,
            LedgerTransactionRow.id,
        )
        rows = (await self._session.scalars(statement)).all()
        return tuple(self._row_to_domain(row) for row in rows)

    async def latest_occurred_at(self, symbol: str) -> datetime | None:
        """读取最后一笔业务发生时间，避免当前版本插入乱序历史。"""

        return cast(
            datetime | None,
            await self._session.scalar(
                select(LedgerTransactionRow.occurred_at)
                .where(LedgerTransactionRow.symbol == normalize_asset_symbol(symbol))
                .order_by(LedgerTransactionRow.occurred_at.desc())
                .limit(1)
            ),
        )

    @staticmethod
    def _row_to_domain(row: LedgerTransactionRow) -> LedgerTransaction:
        """把数据库字符串枚举和数值字段恢复为强类型领域流水。"""

        return LedgerTransaction(
            id=row.id,
            symbol=row.symbol,
            transaction_type=TransactionType(row.transaction_type),
            quantity=row.quantity,
            unit_price=row.unit_price,
            gross_amount=row.gross_amount,
            fee_amount=row.fee_amount,
            cash_amount=row.cash_amount,
            realized_pnl=row.realized_pnl,
            currency=Currency(row.currency),
            occurred_at=row.occurred_at,
            created_at=row.created_at,
            note=row.note,
        )


class SqlAlchemyPurchaseLotRepository:
    """在当前 AsyncSession 中保存 FIFO 买入批次与剩余数量。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, data: PurchaseLotCreate) -> PurchaseLot:
        """新增由期初持仓或买入流水产生的批次。"""

        row = PurchaseLotRow(
            id=data.id,
            opening_transaction_id=data.opening_transaction_id,
            symbol=data.symbol,
            acquired_at=data.acquired_at,
            original_quantity=data.original_quantity,
            remaining_quantity=data.remaining_quantity,
            unit_cost=data.unit_cost,
            created_at=data.created_at,
            updated_at=data.updated_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise PersistenceError(
                f"买入批次或来源交易冲突：{data.id}"
            ) from error
        return self._row_to_domain(row)

    async def list_open_lots(self, symbol: str) -> tuple[PurchaseLot, ...]:
        """只返回剩余数量大于零的批次，并形成稳定 FIFO 顺序。"""

        rows = (
            await self._session.scalars(
                select(PurchaseLotRow)
                .where(
                    PurchaseLotRow.symbol == normalize_asset_symbol(symbol),
                    PurchaseLotRow.remaining_quantity > 0,
                )
                .order_by(
                    PurchaseLotRow.acquired_at,
                    PurchaseLotRow.created_at,
                    PurchaseLotRow.id,
                )
            )
        ).all()
        return tuple(self._row_to_domain(row) for row in rows)

    async def update_remaining(self, lot_id: UUID, remaining: Decimal) -> PurchaseLot:
        """更新卖出后的剩余数量，并让数据库约束再次校验范围。"""

        row = await self._session.get(PurchaseLotRow, lot_id)
        if row is None:
            raise PersistenceError(f"买入批次不存在：{lot_id}")
        if remaining < 0 or remaining > row.original_quantity:
            raise PersistenceError(
                f"买入批次 {lot_id} 的剩余数量必须在 0 到原始数量之间"
            )
        row.remaining_quantity = remaining
        await self._session.flush()
        return self._row_to_domain(row)

    @staticmethod
    def _row_to_domain(row: PurchaseLotRow) -> PurchaseLot:
        """把 ORM Row 转换为不可变买入批次领域模型。"""

        return PurchaseLot(
            id=row.id,
            opening_transaction_id=row.opening_transaction_id,
            symbol=row.symbol,
            acquired_at=row.acquired_at,
            original_quantity=row.original_quantity,
            remaining_quantity=row.remaining_quantity,
            unit_cost=row.unit_cost,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
