"""演示“市场数据服务 → 投资组合计算器”的离线联动。

脚本使用 FakeMarketDataProvider，不访问真实网络。它证明投资组合不需要知道行情来自内存、
HTTP 还是缓存；只要 Provider 返回统一 Quote，后续计算逻辑完全不变。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from finagent.data import FakeMarketDataProvider, MarketDataService
from finagent.portfolio import (
    AssetType,
    Currency,
    Holding,
    PortfolioCalculator,
    Quote,
)


async def check_market_data() -> None:
    """批量获取两条假行情，计算组合并打印请求轨迹。"""

    china_timezone = timezone(timedelta(hours=8))
    market_time = datetime(2026, 7, 14, 15, 0, tzinfo=china_timezone)
    check_time = datetime(2026, 7, 14, 15, 5, tzinfo=china_timezone)
    provider = FakeMarketDataProvider(
        [
            Quote.model_validate(
                {
                    "symbol": "518880",
                    "price": "5.10",
                    "currency": "CNY",
                    "as_of": market_time,
                    "source": "Fake Provider 固定数据",
                    "is_delayed": True,
                }
            ),
            Quote.model_validate(
                {
                    "symbol": "600519",
                    "price": "1600.00",
                    "currency": "CNY",
                    "as_of": market_time,
                    "source": "Fake Provider 固定数据",
                    "is_delayed": True,
                }
            ),
        ]
    )
    service = MarketDataService(
        provider,
        request_timeout_seconds=1,
        max_quote_age=timedelta(minutes=10),
        clock=lambda: check_time,
    )
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

    try:
        # 上层只提供资产代码；Provider 的存储方式与 PortfolioCalculator 完全解耦。
        quotes = await service.get_quotes([holding.symbol for holding in holdings])
        snapshot = PortfolioCalculator(Currency.CNY).calculate(holdings, quotes)

        print(f"实际行情请求顺序：{provider.requested_symbols}")
        print(f"行情来源：{', '.join(quote.source for quote in quotes)}")
        print(f"组合总市值：{snapshot.total_market_value} {snapshot.base_currency}")
        print(f"组合总收益率：{snapshot.total_return_percent}%")
        print(f"组合数据时间：{snapshot.as_of}")
        print(f"包含延迟行情：{snapshot.has_delayed_data}")
    finally:
        await service.close()


def main() -> None:
    """同步脚本入口，由 asyncio 管理事件循环。"""

    asyncio.run(check_market_data())


if __name__ == "__main__":
    main()
