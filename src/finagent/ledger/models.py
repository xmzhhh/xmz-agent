"""交易流水、买入批次和卖出试算的领域模型。

本模块只定义合法数据形状，不访问数据库。金额使用 Decimal，交易时间必须带时区；买入、
卖出和期初持仓的计算由 TransactionService 完成，Repository 只保存已经确定的结果。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from finagent.portfolio.models import (
    Currency,
    DecimalInput,
    FinancialModel,
    Holding,
)
from finagent.portfolio.rounding import ZERO_MONEY, ZERO_PERCENT


class TransactionType(StrEnum):
    """当前账本允许保存的交易事实类型。"""

    OPENING = "opening"
    BUY = "buy"
    SELL = "sell"
    ADJUSTMENT = "adjustment"


def _normalize_symbol(value: Any) -> Any:
    """统一交易模型中的资产代码，确保能稳定匹配目录和持仓。"""

    return value.strip().upper() if isinstance(value, str) else value


def _require_timezone(value: datetime) -> datetime:
    """拒绝无时区时间，避免 FIFO 顺序依赖本机时区。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("交易时间必须包含时区")
    return value


class OpeningPositionRequest(FinancialModel):
    """把已有持仓快照登记为账本的期初批次。

    ``acquired_at`` 应尽量填写真实取得日期，因为基金赎回费用与每批份额的持有时间相关。
    当前小阶段不自动推测历史日期。
    """

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9._-]+$")
    acquired_at: datetime
    note: str | None = Field(default=None, max_length=500)

    _normalize_request_symbol = field_validator("symbol", mode="before")(_normalize_symbol)
    _validate_acquired_at = field_validator("acquired_at")(_require_timezone)


class BuyRequest(FinancialModel):
    """一笔已经确认或准备记账的买入事实。"""

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9._-]+$")
    quantity: DecimalInput = Field(gt=0)
    unit_price: DecimalInput = Field(gt=0)
    fee_amount: DecimalInput = Field(default=ZERO_MONEY, ge=0)
    occurred_at: datetime
    estimated_exit_fee_percent: DecimalInput = Field(
        default=ZERO_PERCENT,
        ge=0,
        le=100,
    )
    note: str | None = Field(default=None, max_length=500)

    _normalize_request_symbol = field_validator("symbol", mode="before")(_normalize_symbol)
    _validate_occurred_at = field_validator("occurred_at")(_require_timezone)


class SellRequest(FinancialModel):
    """一笔卖出试算或确认记账所需的输入。

    场外基金采用“份额赎回”，黄金采用克数卖出，因此这里统一把卖出单位称为 quantity。
    ``fee_amount`` 接收平台展示的预计或最终手续费，不在本阶段硬编码产品费率。
    """

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9._-]+$")
    quantity: DecimalInput = Field(gt=0)
    unit_price: DecimalInput = Field(gt=0)
    fee_amount: DecimalInput = Field(default=ZERO_MONEY, ge=0)
    occurred_at: datetime
    note: str | None = Field(default=None, max_length=500)

    _normalize_request_symbol = field_validator("symbol", mode="before")(_normalize_symbol)
    _validate_occurred_at = field_validator("occurred_at")(_require_timezone)


class LedgerTransactionCreate(FinancialModel):
    """Service 计算完成后交给 Repository 保存的不可变流水。"""

    id: UUID = Field(default_factory=uuid4)
    symbol: str
    transaction_type: TransactionType
    quantity: DecimalInput = Field(gt=0)
    unit_price: DecimalInput = Field(gt=0)
    gross_amount: DecimalInput = Field(gt=0)
    fee_amount: DecimalInput = Field(ge=0)
    cash_amount: DecimalInput = Field(ge=0)
    realized_pnl: DecimalInput | None = None
    currency: Currency
    occurred_at: datetime
    created_at: datetime
    note: str | None = Field(default=None, max_length=500)

    _validate_occurred_at = field_validator("occurred_at")(_require_timezone)
    _validate_created_at = field_validator("created_at")(_require_timezone)


class LedgerTransaction(LedgerTransactionCreate):
    """已经写入账本、可供查询和审计的一笔交易。"""


class PurchaseLotCreate(FinancialModel):
    """由期初持仓或买入交易产生的一批可卖数量。"""

    id: UUID = Field(default_factory=uuid4)
    opening_transaction_id: UUID
    symbol: str
    acquired_at: datetime
    original_quantity: DecimalInput = Field(gt=0)
    remaining_quantity: DecimalInput = Field(ge=0)
    unit_cost: DecimalInput = Field(gt=0)
    created_at: datetime
    updated_at: datetime

    _validate_acquired_at = field_validator("acquired_at")(_require_timezone)
    _validate_created_at = field_validator("created_at")(_require_timezone)
    _validate_updated_at = field_validator("updated_at")(_require_timezone)

    @model_validator(mode="after")
    def remaining_cannot_exceed_original(self) -> Self:
        """阻止批次剩余数量超过最初买入数量。"""

        if self.remaining_quantity > self.original_quantity:
            raise ValueError("批次剩余数量不能超过原始数量")
        return self


class PurchaseLot(PurchaseLotCreate):
    """已经写入数据库的买入批次。"""


class LotConsumption(FinancialModel):
    """卖出试算中从某个 FIFO 批次扣除的数量和对应成本。"""

    lot_id: UUID
    acquired_at: datetime
    quantity: DecimalInput = Field(gt=0)
    unit_cost: DecimalInput = Field(gt=0)
    cost_basis: DecimalInput = Field(ge=0)


class SellPreview(FinancialModel):
    """卖出前的纯计算结果；创建该对象不会修改数据库。"""

    symbol: str
    quantity: DecimalInput = Field(gt=0)
    unit_price: DecimalInput = Field(gt=0)
    gross_amount: DecimalInput = Field(gt=0)
    fee_amount: DecimalInput = Field(ge=0)
    estimated_cash_amount: DecimalInput = Field(ge=0)
    fifo_cost_basis: DecimalInput = Field(ge=0)
    estimated_realized_pnl: DecimalInput
    remaining_quantity: DecimalInput = Field(ge=0)
    remaining_average_cost: DecimalInput | None
    lot_consumptions: tuple[LotConsumption, ...]


class BuyResult(FinancialModel):
    """买入落账后返回的流水、批次和最新持仓。"""

    transaction: LedgerTransaction
    purchase_lot: PurchaseLot
    holding: Holding


class OpeningPositionResult(FinancialModel):
    """已有持仓成功建立期初流水和首个批次后的结果。"""

    transaction: LedgerTransaction
    purchase_lot: PurchaseLot
    holding: Holding


class SellResult(FinancialModel):
    """卖出落账结果；清仓时 holding 为 None。"""

    transaction: LedgerTransaction
    preview: SellPreview
    holding: Holding | None
