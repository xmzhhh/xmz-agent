"""在 PyCharm 中离线验收真实行情多数据源路由。

本脚本使用两个 ``FakeMarketDataProvider``，不会访问 AKShare、GoldAPI 或读取任何 API Key。
它验证一个 ``MarketDataService`` 能否通过 ``RoutingMarketDataProvider`` 按输入顺序取得基金
和黄金行情，并通过请求轨迹证明每个代码只进入正确的子 Provider。

真实外部接口已经在 Phase 4 单独验收；这里固定输入和输出，是为了把“路由逻辑错误”与
“网络或供应商故障”隔离开。脚本无论成功或失败都会从 Service 向下关闭 Router 和两个子
Provider，展示完整的资源生命周期。
"""

import asyncio
from datetime import UTC, datetime

from finagent.data import (
    GOLD_REFERENCE_SYMBOL,
    FakeMarketDataProvider,
    MarketDataError,
    MarketDataService,
    RoutingMarketDataProvider,
)
from finagent.portfolio import Currency, Quote

FUND_SYMBOL = "017811"
_DEMO_TIME = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)


def _make_quote(
    symbol: str,
    *,
    price: str,
    source: str,
    is_delayed: bool,
) -> Quote:
    """构造已通过统一领域模型校验的离线演示行情。"""

    return Quote.model_validate(
        {
            "symbol": symbol,
            "price": price,
            "currency": Currency.CNY,
            "as_of": _DEMO_TIME,
            "source": source,
            "is_delayed": is_delayed,
        }
    )


def _build_default_providers() -> tuple[FakeMarketDataProvider, FakeMarketDataProvider]:
    """创建 PyCharm 默认运行所需的基金和黄金 Fake Provider。"""

    fund_provider = FakeMarketDataProvider(
        [
            _make_quote(
                FUND_SYMBOL,
                price="3.9179",
                source="Fake AKShare 基金净值",
                is_delayed=True,
            )
        ]
    )
    gold_provider = FakeMarketDataProvider(
        [
            _make_quote(
                GOLD_REFERENCE_SYMBOL,
                price="877.49",
                source="Fake GoldAPI 黄金参考价",
                is_delayed=False,
            )
        ]
    )
    return fund_provider, gold_provider


async def check_market_data_routing(
    *,
    fund_provider: FakeMarketDataProvider | None = None,
    gold_provider: FakeMarketDataProvider | None = None,
) -> bool:
    """执行一次基金加黄金的完整离线路由验收。

    Args:
        fund_provider: 可选基金 Fake Provider。省略时使用固定的 017811 演示行情；测试可以
            注入空数据或自定义行情，稳定覆盖失败路径。
        gold_provider: 可选黄金 Fake Provider。省略时使用固定的 ``XAU-CNY-GRAM`` 行情。

    Returns:
        两条行情顺序正确，且两个 Provider 的请求轨迹都符合预期时返回 ``True``；已分类的
        市场数据异常或输入错误返回 ``False``。

    Notes:
        函数只捕获项目已经分类的市场数据异常和输入 ``ValueError``。编程错误继续向外抛出，
        避免验收脚本把真实代码缺陷误报成普通路由失败。
    """

    if fund_provider is None or gold_provider is None:
        default_fund_provider, default_gold_provider = _build_default_providers()
        actual_fund_provider = fund_provider or default_fund_provider
        actual_gold_provider = gold_provider or default_gold_provider
    else:
        actual_fund_provider = fund_provider
        actual_gold_provider = gold_provider

    router = RoutingMarketDataProvider(
        fund_provider=actual_fund_provider,
        fund_symbols={FUND_SYMBOL},
        gold_provider=actual_gold_provider,
    )
    service = MarketDataService(router)
    expected_symbols = (FUND_SYMBOL, GOLD_REFERENCE_SYMBOL)

    print("=== 多数据源路由离线验收 ===")
    print(f"查询顺序：{' → '.join(expected_symbols)}")

    try:
        quotes = await service.get_quotes(expected_symbols)
        actual_symbols = tuple(quote.symbol for quote in quotes)
        fund_requests = actual_fund_provider.requested_symbols
        gold_requests = actual_gold_provider.requested_symbols

        print(f"基金结果：{quotes[0].symbol}，{quotes[0].price} CNY/份，来源：{quotes[0].source}")
        print(f"黄金结果：{quotes[1].symbol}，{quotes[1].price} CNY/克，来源：{quotes[1].source}")
        print(f"基金 Provider 实际请求：{fund_requests}")
        print(f"黄金 Provider 实际请求：{gold_requests}")
        print("真实网络请求：无")

        succeeded = (
            actual_symbols == expected_symbols
            and fund_requests == (FUND_SYMBOL,)
            and gold_requests == (GOLD_REFERENCE_SYMBOL,)
        )
        print(f"路由验收：{'通过' if succeeded else '失败'}")
        return succeeded
    except (MarketDataError, ValueError) as error:
        print(f"错误类型：{type(error).__name__}")
        print(f"错误信息：{error}")
        print("路由验收：失败")
        return False
    finally:
        # 调用方只关闭最高层 Service；关闭操作会沿 Service → Router → 子 Provider 向下传递。
        await service.close()


def main() -> None:
    """创建事件循环运行验收，并用非零退出码表示失败。"""

    succeeded = asyncio.run(check_market_data_routing())
    if not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
