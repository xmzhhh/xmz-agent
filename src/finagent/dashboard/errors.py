"""资产面板应用层的统一异常。

这些异常描述手工价格、演示模式和快照编排规则。它们与具体 Web 框架无关，下一阶段
FastAPI 只负责把稳定的领域异常映射为 HTTP 状态码。
"""

from finagent.portfolio.errors import PortfolioError


class DashboardError(PortfolioError):
    """所有资产面板业务异常的基类。"""


class ManualPriceNotSupportedError(DashboardError):
    """指定资产不使用手工价格估值。"""


class ManualPriceNotFoundError(DashboardError):
    """需要手工价格的持仓尚未录入价格。"""


class ManualPriceStaleError(DashboardError):
    """手工价格已经超过允许使用的最大年龄。"""


class DashboardClockError(DashboardError):
    """应用时钟缺少时区，无法可靠判断价格年龄。"""


class DemoPortfolioUnavailableError(DashboardError):
    """当前运行模式不允许载入匿名演示组合。"""
