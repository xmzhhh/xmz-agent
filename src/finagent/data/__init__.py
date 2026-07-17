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
    UnsupportedMarketDataSymbolError,
)
from finagent.data.fake import FakeMarketDataProvider
from finagent.data.goldapi import GOLD_REFERENCE_SYMBOL, GoldApiMarketDataProvider
from finagent.data.routing import RoutingMarketDataProvider
from finagent.data.service import MarketDataService

__all__ = [
    "AkShareFundNavProvider",
    "DuplicateSymbolRequestError",
    "FakeMarketDataProvider",
    "GOLD_REFERENCE_SYMBOL",
    "GoldApiMarketDataProvider",
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
    "RoutingMarketDataProvider",
    "StaleQuoteError",
    "UnsupportedMarketDataSymbolError",
    "normalize_symbol",
]
