"""交易账本领域的稳定异常。

这些异常描述持仓批次、交易顺序和交易金额之间的业务冲突，不依赖 FastAPI、SQLAlchemy
或具体理财平台。后续网页与 Agent 工具可以把同一异常映射成各自的展示形式。
"""

from finagent.portfolio.errors import PortfolioError


class LedgerError(PortfolioError):
    """所有交易账本业务异常的基类。"""


class LedgerAlreadyInitializedError(LedgerError):
    """当前持仓已经建立了可追踪买入批次，不能重复初始化。"""


class UntrackedHoldingError(LedgerError):
    """持仓来自旧快照但尚无买入批次，必须先初始化期初持仓。"""


class LedgerStateConflictError(LedgerError):
    """持仓数量与未卖完批次数量不一致，拒绝继续扩大错误。"""


class InsufficientHoldingError(LedgerError):
    """卖出数量超过当前可用持仓。"""


class InvalidTradeFeeError(LedgerError):
    """手续费使卖出到账金额为负数，交易输入不成立。"""


class TradeAmountTooSmallError(LedgerError):
    """交易金额按人民币分舍入后为零，无法形成有效流水。"""


class NonChronologicalTransactionError(LedgerError):
    """新交易早于已记录的最后一笔交易，当前版本无法安全重放历史。"""


class FutureTransactionError(LedgerError):
    """交易或期初持仓时间晚于服务端当前时间。"""
