"""投资组合确定性计算引擎的单元测试。

测试覆盖盈利、亏损、舍入、权重、集中度和各类领域错误。所有输入都是固定数据，保证
计算结果可复现，不访问模型、网络或数据库。
"""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from finagent.portfolio import (
    AssetType,
    Currency,
    CurrencyMismatchError,
    DuplicateHoldingError,
    DuplicateQuoteError,
    Holding,
    PortfolioCalculator,
    PortfolioSnapshot,
    Quote,
    QuoteNotFoundError,
)

MARKET_TIME = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)


def make_holding(
    symbol: str = "AAA",
    *,
    name: str = "测试资产",
    asset_type: AssetType = AssetType.STOCK,
    quantity: str = "10",
    average_cost: str = "100",
    currency: Currency = Currency.CNY,
) -> Holding:
    """通过公开校验入口构造测试持仓。"""

    return Holding.model_validate(
        {
            "symbol": symbol,
            "name": name,
            "asset_type": asset_type,
            "quantity": quantity,
            "average_cost": average_cost,
            "currency": currency,
        }
    )


def make_quote(
    symbol: str = "AAA",
    *,
    price: str = "120",
    currency: Currency = Currency.CNY,
    as_of: datetime = MARKET_TIME,
    source: str = "测试行情",
    is_delayed: bool = False,
) -> Quote:
    """通过公开校验入口构造测试行情。"""

    return Quote.model_validate(
        {
            "symbol": symbol,
            "price": price,
            "currency": currency,
            "as_of": as_of,
            "source": source,
            "is_delayed": is_delayed,
        }
    )


def test_single_profitable_position_calculates_all_metrics() -> None:
    """单项盈利持仓应正确计算成本、市值、盈亏、收益率、权重和 HHI。"""

    snapshot = PortfolioCalculator(Currency.CNY).calculate([make_holding()], [make_quote()])

    position = snapshot.positions[0]
    assert position.cost_basis == Decimal("1000.00")
    assert position.market_value == Decimal("1200.00")
    assert position.unrealized_pnl == Decimal("200.00")
    assert position.return_percent == Decimal("20.00")
    assert position.weight_percent == Decimal("100.00")
    assert snapshot.total_return_percent == Decimal("20.00")
    assert snapshot.concentration_hhi == Decimal("10000.00")


def test_losing_position_returns_negative_pnl_and_rate() -> None:
    """价格低于成本时，浮亏金额和收益率应同时为负。"""

    snapshot = PortfolioCalculator(Currency.CNY).calculate(
        [make_holding()], [make_quote(price="80")]
    )

    assert snapshot.total_unrealized_pnl == Decimal("-200.00")
    assert snapshot.total_return_percent == Decimal("-20.00")


def test_money_uses_half_up_rounding_before_summary() -> None:
    """金额应按 ROUND_HALF_UP 保留到分，并保证明细与汇总一致。"""

    snapshot = PortfolioCalculator(Currency.CNY).calculate(
        [make_holding(quantity="3", average_cost="0.335")],
        [make_quote(price="0.345")],
    )

    position = snapshot.positions[0]
    assert position.cost_basis == Decimal("1.01")
    assert position.market_value == Decimal("1.04")
    assert position.unrealized_pnl == Decimal("0.03")
    assert position.return_percent == Decimal("2.97")


def test_rounded_position_weights_always_sum_to_one_hundred() -> None:
    """三项等市值持仓各自舍入后，差额应被修正使权重严格等于 100.00%。"""

    holdings = [make_holding(symbol) for symbol in ("AAA", "BBB", "CCC")]
    quotes = [make_quote(symbol) for symbol in ("AAA", "BBB", "CCC")]

    snapshot = PortfolioCalculator(Currency.CNY).calculate(holdings, quotes)
    weights = [position.weight_percent for position in snapshot.positions]

    assert weights == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]
    assert sum(weights) == Decimal("100.00")
    assert snapshot.concentration_hhi == Decimal("3333.33")


def test_asset_type_weights_group_multiple_positions() -> None:
    """同类资产的持仓权重应合并，供后续资产配置和风险规则使用。"""

    holdings = [
        make_holding("STOCK1", asset_type=AssetType.STOCK),
        make_holding("STOCK2", asset_type=AssetType.STOCK),
        make_holding("GOLD", asset_type=AssetType.GOLD),
    ]
    quotes = [make_quote(symbol) for symbol in ("STOCK1", "STOCK2", "GOLD")]

    snapshot = PortfolioCalculator(Currency.CNY).calculate(holdings, quotes)

    assert snapshot.asset_type_weights == {
        AssetType.STOCK: Decimal("66.67"),
        AssetType.GOLD: Decimal("33.33"),
    }


def test_largest_market_value_becomes_max_position() -> None:
    """最大市值资产应被识别为最大仓位，并保留对应展示权重。"""

    snapshot = PortfolioCalculator(Currency.CNY).calculate(
        [make_holding("SMALL"), make_holding("LARGE", quantity="20")],
        [make_quote("SMALL"), make_quote("LARGE")],
    )

    assert snapshot.max_position_symbol == "LARGE"
    assert snapshot.max_position_weight_percent == Decimal("66.67")


def test_missing_quote_raises_explicit_domain_error() -> None:
    """持仓缺少行情时不能默认为零价格，应明确报告缺失代码。"""

    with pytest.raises(QuoteNotFoundError, match="AAA"):
        PortfolioCalculator(Currency.CNY).calculate([make_holding()], [])


def test_duplicate_holding_is_rejected() -> None:
    """重复持仓会造成市值双重计算，必须在计算前拒绝。"""

    with pytest.raises(DuplicateHoldingError, match="AAA"):
        PortfolioCalculator(Currency.CNY).calculate(
            [make_holding(), make_holding()], [make_quote()]
        )


def test_duplicate_quote_is_rejected() -> None:
    """同一资产有两条行情时无法确定使用哪条，不能依赖列表顺序覆盖。"""

    with pytest.raises(DuplicateQuoteError, match="AAA"):
        PortfolioCalculator(Currency.CNY).calculate(
            [make_holding()], [make_quote(), make_quote(price="121")]
        )


def test_holding_currency_must_match_portfolio_base_currency() -> None:
    """没有汇率模块时，美元持仓不能直接计入人民币组合。"""

    with pytest.raises(CurrencyMismatchError, match="基准币种"):
        PortfolioCalculator(Currency.CNY).calculate(
            [make_holding(currency=Currency.USD)],
            [make_quote(currency=Currency.USD)],
        )


def test_quote_currency_must_match_holding_currency() -> None:
    """持仓成本和行情价格币种不同会产生无意义盈亏，应拒绝计算。"""

    with pytest.raises(CurrencyMismatchError, match="行情使用"):
        PortfolioCalculator(Currency.CNY).calculate(
            [make_holding()], [make_quote(currency=Currency.USD)]
        )


def test_empty_portfolio_returns_consistent_zero_snapshot() -> None:
    """尚未录入持仓时应返回可展示的空快照，而不是发生除零异常。"""

    snapshot = PortfolioCalculator(Currency.CNY).calculate([], [])

    assert snapshot.positions == ()
    assert snapshot.total_market_value == Decimal("0.00")
    assert snapshot.total_return_percent is None
    assert snapshot.max_position_symbol is None
    assert snapshot.as_of is None


def test_snapshot_uses_oldest_quote_time_and_propagates_delay_flag() -> None:
    """组合新鲜度由最旧行情决定，任一行情延迟都应标记整个快照。"""

    china_timezone = timezone(timedelta(hours=8))
    older_time = datetime(2026, 7, 14, 9, 30, tzinfo=china_timezone)
    newer_time = datetime(2026, 7, 14, 2, 0, tzinfo=UTC)
    snapshot = PortfolioCalculator(Currency.CNY).calculate(
        [make_holding("AAA"), make_holding("BBB")],
        [
            make_quote("AAA", as_of=older_time, is_delayed=True),
            make_quote("BBB", as_of=newer_time),
        ],
    )

    # 09:30+08:00 等于 01:30 UTC，早于另一条行情的 02:00 UTC。
    assert snapshot.as_of == older_time
    assert snapshot.has_delayed_data is True


def test_unrelated_extra_quote_does_not_change_portfolio() -> None:
    """行情批次可以包含非持仓资产，多余行情不应进入组合估值。"""

    snapshot = PortfolioCalculator(Currency.CNY).calculate(
        [make_holding("AAA")],
        [make_quote("AAA"), make_quote("UNUSED", price="999")],
    )

    assert [position.symbol for position in snapshot.positions] == ["AAA"]
    assert snapshot.total_market_value == Decimal("1200.00")


def test_snapshot_model_rejects_inconsistent_empty_totals() -> None:
    """即使绕过计算器手工创建快照，空组合也不能携带非零汇总值。"""

    with pytest.raises(ValidationError, match="空投资组合"):
        PortfolioSnapshot.model_validate(
            {
                "base_currency": "CNY",
                "total_cost": "1.00",
                "total_market_value": "0.00",
                "total_unrealized_pnl": "0.00",
                "total_return_percent": None,
                "asset_type_weights": {},
                "max_position_symbol": None,
                "max_position_weight_percent": "0.00",
                "concentration_hhi": "0.00",
                "as_of": None,
                "has_delayed_data": False,
            }
        )
