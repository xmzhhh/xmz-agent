"""资产面板的手工价格、参考价格状态和最终快照模型。

这些 Pydantic 模型是应用服务与未来 FastAPI 之间的数据契约。金融数值继续沿用 portfolio
模块的 Decimal 输入规则，避免在手工价格入口重新引入二进制浮点误差。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from finagent.portfolio.models import (
    Currency,
    DecimalInput,
    FinancialModel,
    PortfolioSnapshot,
    Quote,
)


class ManualPriceInput(FinancialModel):
    """用户录入手工卖出价时唯一允许提交的金融字段。"""

    price: DecimalInput = Field(gt=0)


class ManualPriceRecord(FinancialModel):
    """服务端补全资产代码、币种和录入时间后的手工价格记录。"""

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9._-]+$")
    price: DecimalInput = Field(gt=0)
    currency: Currency
    recorded_at: datetime

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> Any:
        """规范化手工价格代码，保证它可以匹配持仓。"""

        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("recorded_at")
    @classmethod
    def recorded_time_must_have_timezone(cls, value: datetime) -> datetime:
        """拒绝无时区录入时间，否则无法可靠执行 900 秒新鲜度判断。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("手工价格 recorded_at 必须包含时区")
        return value


class GoldReferenceStatus(StrEnum):
    """国际黄金参考价在一次面板快照中的可用状态。"""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_REQUESTED = "not_requested"


class GoldReferenceResult(FinancialModel):
    """国际黄金参考价及其降级状态。"""

    status: GoldReferenceStatus
    quote: Quote | None = None
    message: str | None = None

    @model_validator(mode="after")
    def validate_status_fields(self) -> Self:
        """确保状态、行情和提示语三者不会互相矛盾。"""

        if self.status is GoldReferenceStatus.AVAILABLE:
            if self.quote is None or self.message is not None:
                raise ValueError("参考价可用时必须包含 quote 且不能包含错误提示")
            return self

        if self.quote is not None:
            raise ValueError("参考价不可用或未请求时不能包含 quote")
        if self.status is GoldReferenceStatus.UNAVAILABLE and not self.message:
            raise ValueError("参考价不可用时必须包含提示语")
        if self.status is GoldReferenceStatus.NOT_REQUESTED and self.message is not None:
            raise ValueError("参考价未请求时不应包含错误提示")
        return self


class DashboardSnapshot(FinancialModel):
    """资产组合估值与可选国际黄金参考价组成的完整面板快照。"""

    portfolio: PortfolioSnapshot
    gold_reference: GoldReferenceResult
