"""市场数据访问层的统一异常。

真实行情 SDK 和 HTTP 服务会使用不同异常类型。数据适配器负责把它们转换为本模块的
稳定异常，使投资组合、Agent 和 CLI 不依赖某一家供应商的错误类。
"""


class MarketDataError(RuntimeError):
    """所有市场数据异常的基类。"""


class MarketDataNotFoundError(MarketDataError):
    """数据源中不存在指定资产行情。"""


class MarketDataTimeoutError(MarketDataError):
    """行情请求超过应用允许的最长等待时间。"""


class MarketDataConnectionError(MarketDataError):
    """网络、DNS、代理或 TLS 问题导致无法连接数据源。"""


class MarketDataRateLimitError(MarketDataError):
    """行情供应商拒绝了超过频率或额度的请求。"""


class MarketDataResponseError(MarketDataError):
    """供应商响应缺少字段、代码不匹配或包含其他无效数据。"""


class MarketDataClosedError(MarketDataError):
    """调用方在 Provider 关闭后仍尝试请求行情。"""


class StaleQuoteError(MarketDataError):
    """行情时间早于业务允许的数据新鲜度阈值。"""


class DuplicateSymbolRequestError(MarketDataError):
    """同一次批量请求中重复包含相同的规范化资产代码。"""
