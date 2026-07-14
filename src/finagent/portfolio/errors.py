"""投资组合领域层的统一异常。

这些异常只描述资产与估值规则，不依赖模型 Provider、数据库或行情 SDK。上层 CLI、
Agent 工具和未来 FastAPI 可以捕获同一组稳定异常，而不必理解计算器内部实现。
"""


class PortfolioError(RuntimeError):
    """所有投资组合领域异常的基类。"""


class DuplicateHoldingError(PortfolioError):
    """同一个资产代码在持仓列表中重复出现。"""


class DuplicateQuoteError(PortfolioError):
    """同一个资产代码存在多条行情，无法确定应该使用哪一条。"""


class QuoteNotFoundError(PortfolioError):
    """某项持仓缺少对应行情，无法完成估值。"""


class CurrencyMismatchError(PortfolioError):
    """持仓、行情或投资组合基准币种不一致。"""
