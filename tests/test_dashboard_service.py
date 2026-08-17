"""PortfolioDashboardService 的完整离线编排测试。

所有行情都来自 Fake Provider。测试重点是必要数据失败、可选参考价降级、手工价格边界、
空仓短路和跨仓库联动，不访问 AKShare、GoldAPI 或任何真实持仓。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from finagent.dashboard import (
    DashboardClockError,
    DemoPortfolioUnavailableError,
    GoldReferenceStatus,
    InMemoryManualPriceRepository,
    ManualPriceInput,
    ManualPriceNotFoundError,
    ManualPriceNotSupportedError,
    ManualPriceStaleError,
    PortfolioDashboardService,
)
from finagent.data import (
    FakeMarketDataProvider,
    MarketDataClosedError,
    MarketDataNotFoundError,
    MarketDataService,
)
from finagent.portfolio import (
    Currency,
    DemoPortfolioConflictError,
    HoldingCreate,
    InMemoryHoldingRepository,
    PortfolioCalculator,
    Quote,
)

NOW = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)


class MutableClock:
    """允许测试显式推进时间而不等待真实 900 秒。"""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        """返回当前测试时间。"""

        return self.now


def make_quote(
    symbol: str,
    price: str,
    *,
    as_of: datetime = NOW,
) -> Quote:
    """构造 Dashboard Service 使用的固定人民币行情。"""

    return Quote.model_validate(
        {
            "symbol": symbol,
            "price": price,
            "currency": "CNY",
            "as_of": as_of,
            "source": "Dashboard Fake Provider",
            "is_delayed": False,
        }
    )


def fund_holding() -> HoldingCreate:
    """构造不包含任何真实用户金额的匿名基金持仓。"""

    return HoldingCreate.model_validate(
        {
            "symbol": "017811",
            "quantity": "100",
            "average_cost": "3.50",
            "estimated_exit_fee_percent": "0.50",
        }
    )


def gold_holding() -> HoldingCreate:
    """构造不包含真实用户持仓数据的匿名京东积存金持仓。"""

    return HoldingCreate.model_validate(
        {
            "symbol": "JD-ZS-GOLD",
            "quantity": "2",
            "average_cost": "800",
            "estimated_exit_fee_percent": "0.40",
        }
    )


def manual_price(price: str = "850") -> ManualPriceInput:
    """模拟 API 字符串输入并通过 Pydantic 转换为精确 Decimal。"""

    return ManualPriceInput.model_validate({"price": price})


def build_service(
    quotes: tuple[Quote, ...] = (),
    *,
    clock: MutableClock | None = None,
    max_age_seconds: int = 900,
    demo_enabled: bool = False,
) -> tuple[
    PortfolioDashboardService,
    InMemoryHoldingRepository,
    InMemoryManualPriceRepository,
    FakeMarketDataProvider,
]:
    """组装一个完全离线且依赖可观察的 Dashboard Service。"""

    selected_clock = clock or MutableClock()
    holding_repository = InMemoryHoldingRepository()
    manual_price_repository = InMemoryManualPriceRepository()
    provider = FakeMarketDataProvider(quotes)
    service = PortfolioDashboardService(
        holding_repository,
        manual_price_repository,
        MarketDataService(provider),
        PortfolioCalculator(Currency.CNY),
        manual_price_max_age=timedelta(seconds=max_age_seconds),
        clock=selected_clock,
        demo_enabled=demo_enabled,
    )
    return service, holding_repository, manual_price_repository, provider


async def test_empty_dashboard_does_not_call_market_data() -> None:
    """空仓应返回自洽空快照，不访问基金行情或国际黄金参考价。"""

    service, _, _, provider = build_service()

    dashboard = await service.get_dashboard()

    assert dashboard.portfolio.positions == ()
    assert dashboard.gold_reference.status is GoldReferenceStatus.NOT_REQUESTED
    assert provider.requested_symbols == ()


async def test_fund_only_dashboard_uses_required_market_quote() -> None:
    """基金持仓应查询 017811，但没有黄金持仓时不请求国际黄金参考价。"""

    service, _, _, provider = build_service((make_quote("017811", "4.00"),))
    await service.create_holding(fund_holding())

    dashboard = await service.get_dashboard()

    position = dashboard.portfolio.positions[0]
    assert position.symbol == "017811"
    assert position.market_value == Decimal("400.00")
    assert dashboard.gold_reference.status is GoldReferenceStatus.NOT_REQUESTED
    assert provider.requested_symbols == ("017811",)


async def test_gold_only_dashboard_uses_manual_sell_price_and_optional_reference() -> None:
    """京东黄金按手工卖出价估值，同时单独请求 GoldAPI 参考价用于对比。"""

    service, _, _, provider = build_service((make_quote("XAU-CNY-GRAM", "900"),))
    await service.create_holding(gold_holding())
    record = await service.set_manual_price(
        "JD-ZS-GOLD",
        manual_price(),
    )

    dashboard = await service.get_dashboard()

    position = dashboard.portfolio.positions[0]
    assert record.recorded_at == NOW
    assert position.current_price == Decimal("850")
    assert position.market_value == Decimal("1700.00")
    assert position.quote_source == "用户手工录入的京东金融卖出价"
    assert dashboard.gold_reference.status is GoldReferenceStatus.AVAILABLE
    assert dashboard.gold_reference.quote is not None
    assert dashboard.gold_reference.quote.price == Decimal("900")
    assert provider.requested_symbols == ("XAU-CNY-GRAM",)


async def test_mixed_dashboard_queries_required_fund_before_optional_gold_reference() -> None:
    """混合组合先完成必要基金估值，再查询可选国际黄金参考价。"""

    service, _, _, provider = build_service(
        (make_quote("017811", "4.00"), make_quote("XAU-CNY-GRAM", "900"))
    )
    await service.create_holding(fund_holding())
    await service.create_holding(gold_holding())
    await service.set_manual_price("JD-ZS-GOLD", manual_price())

    dashboard = await service.get_dashboard()

    assert [position.symbol for position in dashboard.portfolio.positions] == [
        "017811",
        "JD-ZS-GOLD",
    ]
    assert dashboard.portfolio.total_market_value == Decimal("2100.00")
    assert provider.requested_symbols == ("017811", "XAU-CNY-GRAM")


async def test_missing_manual_price_rejects_snapshot_before_network_request() -> None:
    """京东卖出价缺失时整份组合失败，并避免浪费任何行情请求。"""

    service, _, _, provider = build_service((make_quote("XAU-CNY-GRAM", "900"),))
    await service.create_holding(gold_holding())

    with pytest.raises(ManualPriceNotFoundError, match="尚未录入"):
        await service.get_dashboard()

    assert provider.requested_symbols == ()


async def test_manual_price_is_valid_at_exact_boundary_and_stale_after_boundary() -> None:
    """恰好 900 秒仍有效，901 秒时必须拒绝继续使用旧手工价格。"""

    clock = MutableClock()
    service, _, _, provider = build_service(
        (make_quote("XAU-CNY-GRAM", "900"),),
        clock=clock,
    )
    await service.create_holding(gold_holding())
    await service.set_manual_price("JD-ZS-GOLD", manual_price())

    clock.now = NOW + timedelta(seconds=900)
    dashboard = await service.get_dashboard()
    assert dashboard.portfolio.total_market_value == Decimal("1700.00")

    clock.now = NOW + timedelta(seconds=901)
    with pytest.raises(ManualPriceStaleError, match="已过期"):
        await service.get_dashboard()
    # 第二次调用在手工价格校验处失败，没有再次请求国际黄金参考价。
    assert provider.requested_symbols == ("XAU-CNY-GRAM",)


async def test_optional_gold_reference_failure_keeps_portfolio_available() -> None:
    """GoldAPI 参考价不可用时不得丢弃已经完成的京东黄金组合估值。"""

    service, _, _, provider = build_service()
    await service.create_holding(gold_holding())
    await service.set_manual_price("JD-ZS-GOLD", manual_price())

    dashboard = await service.get_dashboard()

    assert dashboard.portfolio.total_market_value == Decimal("1700.00")
    assert dashboard.gold_reference.status is GoldReferenceStatus.UNAVAILABLE
    assert dashboard.gold_reference.quote is None
    assert dashboard.gold_reference.message == "国际黄金参考价暂不可用，不影响组合估值"
    assert provider.requested_symbols == ("XAU-CNY-GRAM",)


async def test_required_fund_quote_failure_rejects_whole_snapshot() -> None:
    """必要基金行情失败时不能用残缺数据计算总资产。"""

    service, _, _, provider = build_service()
    await service.create_holding(fund_holding())

    with pytest.raises(MarketDataNotFoundError, match="017811"):
        await service.get_dashboard()

    assert provider.requested_symbols == ("017811",)


async def test_manual_price_only_supports_manual_valuation_asset() -> None:
    """基金和国际参考代码都不能通过手工价格接口绕过既定估值方式。"""

    service, _, _, _ = build_service()

    with pytest.raises(ManualPriceNotSupportedError, match="017811"):
        await service.set_manual_price("017811", manual_price("4"))
    with pytest.raises(ManualPriceNotSupportedError, match="XAU-CNY-GRAM"):
        await service.set_manual_price("XAU-CNY-GRAM", manual_price("900"))


async def test_delete_gold_holding_also_clears_manual_price() -> None:
    """删除京东黄金持仓时应同步清除孤立价格，避免以后误用旧记录。"""

    service, _, manual_prices, _ = build_service()
    await service.create_holding(gold_holding())
    await service.set_manual_price("JD-ZS-GOLD", manual_price())

    await service.delete_holding("JD-ZS-GOLD")

    assert await manual_prices.get_price("JD-ZS-GOLD") is None
    with pytest.raises(ManualPriceNotFoundError):
        await service.get_manual_price("JD-ZS-GOLD")


async def test_fake_mode_loads_complete_anonymous_demo_and_rejects_second_load() -> None:
    """Fake 模式一次载入两项匿名持仓和手工价，第二次不得覆盖现有状态。"""

    service, _, _, provider = build_service(
        (make_quote("017811", "4.00"), make_quote("XAU-CNY-GRAM", "900")),
        demo_enabled=True,
    )

    holdings = await service.load_demo()
    dashboard = await service.get_dashboard()

    assert [holding.symbol for holding in holdings] == ["017811", "JD-ZS-GOLD"]
    assert dashboard.portfolio.total_market_value == Decimal("2100.00")
    assert provider.requested_symbols == ("017811", "XAU-CNY-GRAM")
    with pytest.raises(DemoPortfolioConflictError):
        await service.load_demo()


async def test_real_mode_rejects_demo_without_writing_state() -> None:
    """未启用演示功能时应在任何仓库写入前立即失败。"""

    service, holdings, manual_prices, _ = build_service(demo_enabled=False)

    with pytest.raises(DemoPortfolioUnavailableError, match="Fake"):
        await service.load_demo()

    assert await holdings.list_holdings() == ()
    assert await manual_prices.get_price("JD-ZS-GOLD") is None


async def test_dashboard_rejects_naive_clock_and_negative_max_age() -> None:
    """无时区时钟和负新鲜度阈值都会破坏价格年龄语义，应尽早失败。"""

    with pytest.raises(ValueError, match="不能为负数"):
        build_service(max_age_seconds=-1)

    naive_clock = MutableClock(datetime(2026, 7, 22, 10, 0))
    service, _, _, _ = build_service(clock=naive_clock)
    await service.create_holding(gold_holding())

    with pytest.raises(DashboardClockError, match="必须返回带时区"):
        await service.set_manual_price("JD-ZS-GOLD", manual_price())


async def test_service_close_releases_market_provider() -> None:
    """Web 应用关闭时，Dashboard Service 应把资源释放传递到底层 Provider。"""

    service, _, _, provider = build_service((make_quote("017811", "4.00"),))

    await service.close()

    with pytest.raises(MarketDataClosedError):
        await provider.get_quote("017811")
