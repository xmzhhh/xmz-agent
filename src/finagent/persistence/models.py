"""Phase 7 的 SQLAlchemy ORM 持久化模型。

这些类描述“数据如何存入关系数据库”，不承担持仓计算、费率判断或 API 校验。领域层继续
使用 Pydantic 模型；Repository 负责在 ORM Row 与领域模型之间转换。当前持仓是便于面板
查询的状态投影，交易流水和买入批次则保留变化历史，两者将在同一数据库事务中更新。
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime, TypeDecorator

from finagent.persistence.database import Base

# 数量、单价和金额统一保留最多 8 位小数。数据库中的 scale 是存储上限，不代表所有页面
# 都展示 8 位；人民币金额仍会在领域计算层按产品规则舍入。
FINANCIAL_PRECISION = 28
FINANCIAL_SCALE = 8
PERCENT_PRECISION = 9
PERCENT_SCALE = 6


def _utc_now() -> datetime:
    """返回带时区的 UTC 当前时间，供 ORM 默认值使用。"""

    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """在 SQLite 中以无时区 UTC 值存储、读取时恢复 UTC 时区。

    SQLite 没有真正的 ``TIMESTAMP WITH TIME ZONE``。只声明 ``timezone=True`` 仍可能在读取
    后得到无时区 ``datetime``，从而破坏行情新鲜度和持有天数计算。本类型在写入前强制要求
    时区并统一转成 UTC，读取后再补回 UTC；数据库里的所有时间因此拥有同一语义。
    """

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        _dialect: Dialect,
    ) -> datetime | None:
        """校验应用时间并转换成 SQLite 可保存的 UTC 无时区值。"""

        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("持久化时间必须包含时区")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        _dialect: Dialect,
    ) -> datetime | None:
        """把 SQLite 读出的 UTC 无时区值恢复成带 UTC 时区的时间。"""

        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class HoldingRow(Base):
    """当前持仓状态投影；清仓后删除，但交易历史继续保留。"""

    __tablename__ = "holding_positions"
    __table_args__ = (
        CheckConstraint("length(symbol) BETWEEN 1 AND 32", name="symbol_length"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("average_cost > 0", name="average_cost_positive"),
        CheckConstraint(
            "estimated_exit_fee_percent BETWEEN 0 AND 100",
            name="exit_fee_percent_range",
        ),
        CheckConstraint(
            "currency IN ('CNY', 'USD', 'HKD')",
            name="currency_supported",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    average_cost: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    estimated_exit_fee_percent: Mapped[Decimal] = mapped_column(
        Numeric(PERCENT_PRECISION, PERCENT_SCALE),
        nullable=False,
        default=Decimal("0"),
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )


class ManualPriceRow(Base):
    """用户手工录入的可成交价格；是否过期仍由 Dashboard Service 判断。"""

    __tablename__ = "manual_prices"
    __table_args__ = (
        CheckConstraint("length(symbol) BETWEEN 1 AND 32", name="symbol_length"),
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint(
            "currency IN ('CNY', 'USD', 'HKD')",
            name="currency_supported",
        ),
    )

    # 手工价格允许先于持仓录入，因此这里不能外键关联 holding_positions。
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    price: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class LedgerTransactionRow(Base):
    """不可变交易流水，保存用户确认后的交易事实和当时的计算结果。

    ``cash_amount`` 始终为正数：买入表示实际支出，卖出表示实际到账；资金方向由
    ``transaction_type`` 判断。``realized_pnl`` 只在卖出时填写，其他类型保持 ``NULL``。
    """

    __tablename__ = "ledger_transactions"
    __table_args__ = (
        CheckConstraint("length(symbol) BETWEEN 1 AND 32", name="symbol_length"),
        CheckConstraint(
            "transaction_type IN ('opening', 'buy', 'sell', 'adjustment')",
            name="transaction_type_supported",
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
        CheckConstraint("gross_amount > 0", name="gross_amount_positive"),
        CheckConstraint("fee_amount >= 0", name="fee_amount_non_negative"),
        CheckConstraint("cash_amount >= 0", name="cash_amount_non_negative"),
        CheckConstraint(
            "currency IN ('CNY', 'USD', 'HKD')",
            name="currency_supported",
        ),
        CheckConstraint(
            "note IS NULL OR length(note) <= 500",
            name="note_length",
        ),
        Index("ix_ledger_transactions_symbol_occurred_at", "symbol", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    gross_amount: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    fee_amount: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
        default=Decimal("0"),
    )
    cash_amount: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    realized_pnl: Mapped[Decimal | None] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=True,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class PurchaseLotRow(Base):
    """一次初始持仓或买入形成的批次，以及尚未卖出的剩余数量。"""

    __tablename__ = "purchase_lots"
    __table_args__ = (
        CheckConstraint("length(symbol) BETWEEN 1 AND 32", name="symbol_length"),
        CheckConstraint("original_quantity > 0", name="original_quantity_positive"),
        CheckConstraint("remaining_quantity >= 0", name="remaining_quantity_non_negative"),
        CheckConstraint(
            "remaining_quantity <= original_quantity",
            name="remaining_not_above_original",
        ),
        CheckConstraint("unit_cost > 0", name="unit_cost_positive"),
        Index("ix_purchase_lots_symbol_acquired_at", "symbol", "acquired_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    opening_transaction_id: Mapped[UUID] = mapped_column(
        Uuid(),
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    original_quantity: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    remaining_quantity: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    # unit_cost 已分摊买入费用，用于后续计算卖出部分对应的成本。
    unit_cost: Mapped[Decimal] = mapped_column(
        Numeric(FINANCIAL_PRECISION, FINANCIAL_SCALE),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )
