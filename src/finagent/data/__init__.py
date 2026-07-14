"""FinAgent 市场数据访问层的公共接口。"""

from finagent.data.base import MarketDataProvider, normalize_symbol
from finagent.data.errors import (
    DuplicateSymbolRequestError,
    MarketDataClosedError,
    MarketDataConnectionError,
    MarketDataError,
    MarketDataNotFoundError,
    MarketDataRateLimitError,
    MarketDataResponseError,
    MarketDataTimeoutError,
    StaleQuoteError,
)
from finagent.data.fake import FakeMarketDataProvider
from finagent.data.service import MarketDataService

__all__ = [
    "DuplicateSymbolRequestError",
    "FakeMarketDataProvider",
    "MarketDataClosedError",
    "MarketDataConnectionError",
    "MarketDataError",
    "MarketDataNotFoundError",
    "MarketDataProvider",
    "MarketDataRateLimitError",
    "MarketDataResponseError",
    "MarketDataService",
    "MarketDataTimeoutError",
    "StaleQuoteError",
    "normalize_symbol",
]
