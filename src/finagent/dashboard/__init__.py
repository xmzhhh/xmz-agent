"""FinAgent 资产面板应用层的公共接口。"""

from finagent.dashboard.errors import (
    DashboardClockError,
    DashboardError,
    DemoPortfolioUnavailableError,
    ManualPriceNotFoundError,
    ManualPriceNotSupportedError,
    ManualPriceStaleError,
)
from finagent.dashboard.manual_prices import (
    InMemoryManualPriceRepository,
    ManualPriceRepository,
)
from finagent.dashboard.models import (
    DashboardSnapshot,
    GoldReferenceResult,
    GoldReferenceStatus,
    ManualPriceInput,
    ManualPriceRecord,
)
from finagent.dashboard.service import (
    ANONYMOUS_DEMO_GOLD_PRICE,
    ANONYMOUS_DEMO_HOLDINGS,
    JD_GOLD_SYMBOL,
    MANUAL_PRICE_SOURCE,
    PortfolioDashboardService,
)
from finagent.dashboard.unit_of_work import (
    DashboardUnitOfWork,
    DashboardUnitOfWorkFactory,
    InMemoryDashboardUnitOfWorkFactory,
)

__all__ = [
    "ANONYMOUS_DEMO_GOLD_PRICE",
    "ANONYMOUS_DEMO_HOLDINGS",
    "DashboardClockError",
    "DashboardError",
    "DashboardSnapshot",
    "DashboardUnitOfWork",
    "DashboardUnitOfWorkFactory",
    "DemoPortfolioUnavailableError",
    "GoldReferenceResult",
    "GoldReferenceStatus",
    "InMemoryManualPriceRepository",
    "InMemoryDashboardUnitOfWorkFactory",
    "JD_GOLD_SYMBOL",
    "MANUAL_PRICE_SOURCE",
    "ManualPriceInput",
    "ManualPriceNotFoundError",
    "ManualPriceNotSupportedError",
    "ManualPriceRecord",
    "ManualPriceRepository",
    "ManualPriceStaleError",
    "PortfolioDashboardService",
]
