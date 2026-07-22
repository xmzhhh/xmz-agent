"""资产、行情和投资组合估值结果的领域模型。

本模块只表达“什么是合法的金融数据”，不负责访问行情接口或执行估值公式。所有外部
数据进入计算引擎前都要先通过这些 Pydantic 模型校验，避免负价格、无时区行情和
二进制浮点误差继续传播。
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from finagent.portfolio.rounding import ONE_HUNDRED_PERCENT, ZERO_MONEY, ZERO_PERCENT


def _reject_binary_float(value: Any) -> Any:
    """拒绝 float 和 bool，允许字符串、整数或 Decimal 进入精确转换。

    ``Decimal(0.1)`` 会把 float 已经产生的二进制误差完整保留下来，因此金融边界明确
    拒绝 float。JSON 或表单中的小数字符串可以安全转换为 Decimal；整数本身也是精确值。
    bool 在 Python 中是 int 的子类，也必须显式拒绝，防止 ``True`` 被当成金额 1。
    """

    if isinstance(value, (bool, float)):
        raise ValueError("金融数值必须使用字符串、整数或 Decimal，不能使用 bool 或 float")
    return value


type DecimalInput = Annotated[Decimal, BeforeValidator(_reject_binary_float)]


class FinancialModel(BaseModel):
    """投资组合领域模型共享的严格配置。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class AssetType(StrEnum):
    """当前版本支持的资产类别。"""

    STOCK = "stock"
    FUND = "fund"
    GOLD = "gold"
    BOND = "bond"
    CASH = "cash"
    OTHER = "other"


class Currency(StrEnum):
    """第一版允许记录的币种；本阶段不负责汇率换算。"""

    CNY = "CNY"
    USD = "USD"
    HKD = "HKD"


class Holding(FinancialModel):
    """用户持有的一项资产及其成本信息。

    Attributes:
        symbol: 资产唯一代码，创建时自动去除空格并转成大写。
        name: 面向用户展示的资产名称。
        asset_type: 股票、基金、黄金等资产类别。
        quantity: 持有数量，必须大于零。
        average_cost: 每单位平均成本，必须大于零。
        estimated_exit_fee_percent: 预计卖出费率，使用百分数语义；例如 0.5 表示 0.5%。
        currency: 成本使用的币种。
    """

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9._-]+$")
    name: str = Field(min_length=1, max_length=100)
    asset_type: AssetType
    quantity: DecimalInput = Field(gt=0)
    average_cost: DecimalInput = Field(gt=0)
    estimated_exit_fee_percent: DecimalInput = Field(default=ZERO_PERCENT, ge=0, le=100)
    currency: Currency

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> Any:
        """统一资产代码大小写，保证持仓可以稳定匹配行情。"""

        return value.strip().upper() if isinstance(value, str) else value


class Quote(FinancialModel):
    """某项资产在明确时间和来源下的价格快照。"""

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9._-]+$")
    price: DecimalInput = Field(gt=0)
    currency: Currency
    as_of: datetime
    source: str = Field(min_length=1, max_length=200)
    is_delayed: bool = False

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> Any:
        """使用与持仓相同的资产代码规范化规则。"""

        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("as_of")
    @classmethod
    def market_time_must_have_timezone(cls, value: datetime) -> datetime:
        """拒绝无时区行情，避免无法判断数据对应哪个市场时刻。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("行情时间 as_of 必须包含时区")
        return value


class ValuedHolding(FinancialModel):
    """持仓与行情匹配后得到的单项资产估值结果。

    ``market_value``、``unrealized_pnl`` 和 ``return_percent`` 是未扣卖出费的毛口径；
    ``net_*`` 字段是假设按当前价格立即卖出并扣除预计费用后的净口径。两组字段同时保留，
    页面才能清楚解释“账面价值”和“预计真正到账金额”的区别。
    """

    symbol: str
    name: str
    asset_type: AssetType
    currency: Currency
    quantity: DecimalInput
    average_cost: DecimalInput
    current_price: DecimalInput
    cost_basis: DecimalInput
    market_value: DecimalInput
    unrealized_pnl: DecimalInput
    return_percent: DecimalInput
    estimated_exit_fee_percent: DecimalInput = Field(ge=0, le=100)
    estimated_exit_fee: DecimalInput = Field(ge=0)
    net_liquidation_value: DecimalInput = Field(ge=0)
    net_unrealized_pnl: DecimalInput
    net_return_percent: DecimalInput
    weight_percent: DecimalInput = Field(ge=0, le=100)
    quote_as_of: datetime
    quote_source: str
    quote_is_delayed: bool


class PortfolioSnapshot(FinancialModel):
    """同一基准币种下的一次完整投资组合估值快照。

    ``as_of`` 取所有行情时间中的最早值，因为整个组合的可靠时间上限由最旧行情决定。
    HHI 使用百分比权重平方和，范围为 0～10000：越接近 10000，持仓越集中。
    """

    base_currency: Currency
    positions: tuple[ValuedHolding, ...] = ()
    total_cost: DecimalInput = Field(ge=0)
    total_market_value: DecimalInput = Field(ge=0)
    total_unrealized_pnl: DecimalInput
    total_return_percent: DecimalInput | None
    total_estimated_exit_fee: DecimalInput = Field(ge=0)
    total_net_liquidation_value: DecimalInput = Field(ge=0)
    total_net_unrealized_pnl: DecimalInput
    total_net_return_percent: DecimalInput | None
    asset_type_weights: dict[AssetType, DecimalInput]
    max_position_symbol: str | None
    max_position_weight_percent: DecimalInput = Field(ge=0, le=100)
    concentration_hhi: DecimalInput = Field(ge=0, le=10000)
    as_of: datetime | None
    has_delayed_data: bool

    @model_validator(mode="after")
    def validate_snapshot_invariants(self) -> Self:
        """确保空组合和非空组合内部字段保持一致。"""

        if not self.positions:
            if (
                self.total_cost != ZERO_MONEY
                or self.total_market_value != ZERO_MONEY
                or self.total_unrealized_pnl != ZERO_MONEY
                or self.total_return_percent is not None
                or self.total_estimated_exit_fee != ZERO_MONEY
                or self.total_net_liquidation_value != ZERO_MONEY
                or self.total_net_unrealized_pnl != ZERO_MONEY
                or self.total_net_return_percent is not None
                or self.asset_type_weights
                or self.max_position_symbol is not None
                or self.max_position_weight_percent != ZERO_PERCENT
                or self.concentration_hhi != ZERO_PERCENT
                or self.as_of is not None
                or self.has_delayed_data
            ):
                raise ValueError("空投资组合的汇总字段必须使用空值或零值")
            return self

        if self.total_market_value <= ZERO_MONEY or self.as_of is None:
            raise ValueError("非空投资组合必须包含正市值和行情时间")

        position_weight_sum = sum(
            (position.weight_percent for position in self.positions),
            start=ZERO_PERCENT,
        )
        if position_weight_sum != ONE_HUNDRED_PERCENT:
            raise ValueError("非空投资组合的持仓权重之和必须等于 100.00%")

        type_weight_sum = sum(self.asset_type_weights.values(), start=ZERO_PERCENT)
        if type_weight_sum != ONE_HUNDRED_PERCENT:
            raise ValueError("非空投资组合的资产类别权重之和必须等于 100.00%")

        if self.max_position_symbol not in {position.symbol for position in self.positions}:
            raise ValueError("最大仓位代码必须存在于估值持仓中")
        return self
