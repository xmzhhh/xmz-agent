"""Step 03：在 PyCharm 中直接演示投资组合计算引擎。

脚本使用完全虚构的持仓和固定行情，不访问网络，也不读取个人资产。它用于人工观察
结构化快照，pytest 仍负责精确断言每个公式和错误边界。
"""

from datetime import datetime, timedelta, timezone

from finagent.portfolio import (
    AssetType,
    Currency,
    Holding,
    PortfolioCalculator,
    Quote,
)


def main() -> None:
    """构造两项模拟持仓并打印估值、权重和集中度结果。"""

    china_timezone = timezone(timedelta(hours=8))
    market_time = datetime(2026, 7, 14, 15, 0, tzinfo=china_timezone)
    holdings = [
        Holding.model_validate(
            {
                "symbol": "518880",
                "name": "黄金 ETF（模拟）",
                "asset_type": AssetType.GOLD,
                "quantity": "1000",
                "average_cost": "4.80",
                "currency": Currency.CNY,
            }
        ),
        Holding.model_validate(
            {
                "symbol": "600519",
                "name": "示例股票（模拟）",
                "asset_type": AssetType.STOCK,
                "quantity": "10",
                "average_cost": "1500.00",
                "currency": Currency.CNY,
            }
        ),
    ]
    quotes = [
        Quote.model_validate(
            {
                "symbol": "518880",
                "price": "5.10",
                "currency": Currency.CNY,
                "as_of": market_time,
                "source": "FinAgent 固定演示数据",
                "is_delayed": True,
            }
        ),
        Quote.model_validate(
            {
                "symbol": "600519",
                "price": "1600.00",
                "currency": Currency.CNY,
                "as_of": market_time,
                "source": "FinAgent 固定演示数据",
                "is_delayed": True,
            }
        ),
    ]

    snapshot = PortfolioCalculator(Currency.CNY).calculate(holdings, quotes)

    print("FinAgent 模拟投资组合估值结果")
    print(f"总成本：{snapshot.total_cost} {snapshot.base_currency}")
    print(f"总市值：{snapshot.total_market_value} {snapshot.base_currency}")
    print(f"浮动盈亏：{snapshot.total_unrealized_pnl} {snapshot.base_currency}")
    print(f"总收益率：{snapshot.total_return_percent}%")
    print(f"最大仓位：{snapshot.max_position_symbol}")
    print(f"集中度 HHI：{snapshot.concentration_hhi}")
    print(f"组合数据时间：{snapshot.as_of}")
    print(f"包含延迟数据：{snapshot.has_delayed_data}")
    print("\n持仓明细：")
    for position in snapshot.positions:
        print(
            f"- {position.name}：市值 {position.market_value}，"
            f"盈亏 {position.unrealized_pnl}，收益率 {position.return_percent}%，"
            f"权重 {position.weight_percent}%"
        )


if __name__ == "__main__":
    main()
