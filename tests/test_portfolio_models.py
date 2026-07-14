"""资产与行情领域模型的边界校验测试。

这些测试确保不可信输入在进入估值公式前就被拒绝，尤其防止 float 精度误差、负数金额、
无时区行情和未知字段悄悄进入投资组合计算。
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from finagent.portfolio import AssetType, Currency, Holding, Quote


def valid_holding_data() -> dict[str, object]:
    """返回可按测试场景修改的合法持仓字典。"""

    return {
        "symbol": "518880",
        "name": "黄金 ETF",
        "asset_type": "gold",
        "quantity": "1000",
        "average_cost": "4.80",
        "currency": "CNY",
    }


def valid_quote_data() -> dict[str, object]:
    """返回可按测试场景修改的合法行情字典。"""

    return {
        "symbol": "518880",
        "price": "5.10",
        "currency": "CNY",
        "as_of": datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        "source": "测试行情",
        "is_delayed": True,
    }


def test_holding_normalizes_symbol_and_parses_exact_decimal() -> None:
    """资产代码应统一大小写，字符串小数应精确转换为 Decimal。"""

    data = valid_holding_data()
    data["symbol"] = "  fund.abc  "

    holding = Holding.model_validate(data)

    assert holding.symbol == "FUND.ABC"
    assert holding.quantity == Decimal("1000")
    assert holding.average_cost == Decimal("4.80")
    assert holding.asset_type is AssetType.GOLD


def test_holding_rejects_float_financial_value() -> None:
    """float 已可能携带二进制误差，不能进入金融领域模型。"""

    data = valid_holding_data()
    data["average_cost"] = 4.8

    with pytest.raises(ValidationError, match="不能使用 bool 或 float"):
        Holding.model_validate(data)


def test_holding_rejects_boolean_as_quantity() -> None:
    """bool 是 int 的子类，但不能被错误解释成持有数量 1。"""

    data = valid_holding_data()
    data["quantity"] = True

    with pytest.raises(ValidationError, match="不能使用 bool 或 float"):
        Holding.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [("quantity", "0"), ("quantity", "-1"), ("average_cost", "0")],
)
def test_holding_rejects_non_positive_quantity_or_cost(field: str, value: str) -> None:
    """零数量、负数量和零成本都会破坏持仓或收益率语义，应被拒绝。"""

    data = valid_holding_data()
    data[field] = value

    with pytest.raises(ValidationError, match="greater_than"):
        Holding.model_validate(data)


def test_holding_rejects_unknown_field() -> None:
    """未知字段可能来自拼写错误，不能被静默忽略。"""

    data = valid_holding_data()
    data["ammount"] = "1000"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        Holding.model_validate(data)


def test_quote_requires_timezone_aware_market_time() -> None:
    """无时区时间无法可靠比较不同市场的数据新鲜度。"""

    data = valid_quote_data()
    data["as_of"] = datetime(2026, 7, 14, 10, 0)

    with pytest.raises(ValidationError, match="必须包含时区"):
        Quote.model_validate(data)


@pytest.mark.parametrize("price", ["0", "NaN", "Infinity"])
def test_quote_rejects_non_positive_or_non_finite_price(price: str) -> None:
    """零价格、NaN 和无穷大都不能进入当前版本的资产估值。"""

    data = valid_quote_data()
    data["price"] = price

    with pytest.raises(ValidationError, match="greater_than|finite_number"):
        Quote.model_validate(data)


def test_quote_requires_non_empty_source() -> None:
    """来源不明的价格不能作为可追溯投资结论的依据。"""

    data = valid_quote_data()
    data["source"] = "   "

    with pytest.raises(ValidationError, match="string_too_short"):
        Quote.model_validate(data)


def test_quote_keeps_currency_and_delay_metadata() -> None:
    """合法行情应保留币种、来源时间和延迟标记。"""

    quote = Quote.model_validate(valid_quote_data())

    assert quote.currency is Currency.CNY
    assert quote.as_of.tzinfo is UTC
    assert quote.is_delayed is True
