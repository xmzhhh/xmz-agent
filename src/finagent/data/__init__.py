"""FinAgent 市场数据访问层的公共接口。"""

from finagent.data.akshare import AkShareFundNavProvider
from finagent.data.base import MarketDataProvider, normalize_symbol
from finagent.data.cache import QuoteCache
from finagent.data.errors import (
    DuplicateSymbolRequestError,
    MarketDataAuthenticationError,
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
    "AkShareFundNavProvider",
    "DuplicateSymbolRequestError",
    "FakeMarketDataProvider",
    "MarketDataAuthenticationError",
    "MarketDataClosedError",
    "MarketDataConnectionError",
    "MarketDataError",
    "MarketDataNotFoundError",
    "MarketDataProvider",
    "MarketDataRateLimitError",
    "MarketDataResponseError",
    "MarketDataService",
    "MarketDataTimeoutError",
    "QuoteCache",
    "StaleQuoteError",
    "normalize_symbol",
]
