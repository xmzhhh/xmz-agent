"""根据应用配置装配资产面板的全部运行时依赖。

组合根是唯一知道“Fake 模式用哪些实现、Real 模式用哪些实现”的位置。FastAPI 路由只依赖
``PortfolioDashboardService``，因此切换数据源时不需要修改 HTTP 接口或金融计算代码。
"""

from datetime import UTC, datetime, timedelta

from finagent.core.config import Settings
from finagent.dashboard import InMemoryManualPriceRepository, PortfolioDashboardService
from finagent.data import (
    AkShareFundNavProvider,
    FakeMarketDataProvider,
    GoldApiMarketDataProvider,
    MarketDataProvider,
    MarketDataService,
    RoutingMarketDataProvider,
)
from finagent.portfolio import (
    Currency,
    InMemoryHoldingRepository,
    PortfolioCalculator,
    Quote,
)

SUPPORTED_FUND_SYMBOLS = frozenset({"017811"})


def _build_fake_quotes(now: datetime) -> tuple[Quote, ...]:
    """生成不包含真实用户信息、也不访问网络的固定演示行情。"""

    return (
        Quote.model_validate(
            {
                "symbol": "017811",
                "price": "4.00",
                "currency": "CNY",
                "as_of": now,
                "source": "Fake Provider 固定基金净值",
                "is_delayed": True,
            }
        ),
        Quote.model_validate(
            {
                "symbol": "XAU-CNY-GRAM",
                "price": "900.00",
                "currency": "CNY",
                "as_of": now,
                "source": "Fake Provider 固定国际黄金参考价",
                "is_delayed": True,
            }
        ),
    )


def build_market_data_service(settings: Settings) -> MarketDataService:
    """根据 Fake/Real 模式创建统一 MarketDataService。"""

    # 两个分支返回不同的具体 Provider；把变量声明为共同协议，既保留静态类型检查，
    # 也强调上层 Service 不依赖 Fake 或 Real 的实现细节。
    provider: MarketDataProvider
    if settings.market_data_mode == "fake":
        provider = FakeMarketDataProvider(_build_fake_quotes(datetime.now(UTC)))
    else:
        # Settings 已保证 Real 模式配置 GoldAPI Key；两个真实 Provider 仍只在收到请求时访问网络。
        provider = RoutingMarketDataProvider(
            fund_provider=AkShareFundNavProvider(),
            fund_symbols=SUPPORTED_FUND_SYMBOLS,
            gold_provider=GoldApiMarketDataProvider(settings),
        )
    return MarketDataService(provider)


def build_dashboard_service(settings: Settings) -> PortfolioDashboardService:
    """装配一次进程内共享的资产面板服务及内存仓库。"""

    return PortfolioDashboardService(
        InMemoryHoldingRepository(),
        InMemoryManualPriceRepository(),
        build_market_data_service(settings),
        PortfolioCalculator(Currency.CNY),
        manual_price_max_age=timedelta(seconds=settings.manual_gold_price_max_age_seconds),
        demo_enabled=settings.market_data_mode == "fake",
    )
