"""市场数据的应用服务：超时、批量顺序与新鲜度检查。

Provider 只负责“如何从某个来源取得一条行情”；本服务负责所有数据源都必须遵守的应用
规则。把规则放在 Provider 外层，真实数据适配器就不会重复实现超时和数据年龄判断。
"""

import asyncio
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from finagent.data.base import MarketDataProvider, normalize_symbol
from finagent.data.errors import (
    DuplicateSymbolRequestError,
    MarketDataResponseError,
    MarketDataTimeoutError,
    StaleQuoteError,
)
from finagent.portfolio import Quote

type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """返回带时区的当前 UTC 时间；独立函数便于测试注入固定时钟。"""

    return datetime.now(UTC)


class MarketDataService:
    """为任意行情 Provider 增加统一应用级保护。

    Args:
        provider: 实现统一协议的行情适配器。
        request_timeout_seconds: 单条行情的最长等待秒数。
        max_quote_age: 可选最大行情年龄；省略表示本阶段不检查陈旧程度。
        clock: 返回带时区当前时间的函数，测试可注入固定值避免依赖系统时间。
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        request_timeout_seconds: float = 5,
        max_quote_age: timedelta | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds 必须是有限的正数")
        if max_quote_age is not None and max_quote_age < timedelta(0):
            raise ValueError("max_quote_age 不能为负数")

        self._provider = provider
        self._request_timeout_seconds = request_timeout_seconds
        self._max_quote_age = max_quote_age
        self._clock = clock

    async def get_quote(self, symbol: str) -> Quote:
        """取得一条行情，并统一执行超时、代码匹配和新鲜度检查。"""

        normalized_symbol = normalize_symbol(symbol)
        try:
            # asyncio.timeout 会取消超时的协程，使慢请求不会继续占用连接和任务资源。
            async with asyncio.timeout(self._request_timeout_seconds):
                quote = await self._provider.get_quote(normalized_symbol)
        except TimeoutError as error:
            raise MarketDataTimeoutError(
                f"查询 {normalized_symbol} 超过 {self._request_timeout_seconds:g} 秒"
            ) from error

        if quote.symbol != normalized_symbol:
            raise MarketDataResponseError(
                f"请求 {normalized_symbol}，Provider 却返回 {quote.symbol}"
            )
        self._validate_freshness(quote)
        return quote

    async def get_quotes(self, symbols: Sequence[str]) -> tuple[Quote, ...]:
        """按请求顺序依次获取多条行情，并拒绝重复代码。

        当前选择串行请求，便于遵守免费行情源的限流并保持错误顺序确定。未来确认供应商
        支持并发或批量接口后，可以只替换这里的调度策略，而不改变调用方。
        """

        normalized_symbols = tuple(normalize_symbol(symbol) for symbol in symbols)
        if len(normalized_symbols) != len(set(normalized_symbols)):
            raise DuplicateSymbolRequestError("同一次行情请求不能包含重复资产代码")

        results: list[Quote] = []
        for symbol in normalized_symbols:
            results.append(await self.get_quote(symbol))
        return tuple(results)

    def _validate_freshness(self, quote: Quote) -> None:
        """当配置最大年龄时，拒绝早于阈值的陈旧行情。"""

        if self._max_quote_age is None:
            return

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise MarketDataResponseError("MarketDataService 的 clock 必须返回带时区时间")

        age = now - quote.as_of
        if age > self._max_quote_age:
            raise StaleQuoteError(
                f"行情 {quote.symbol} 已陈旧 {age}，超过允许值 {self._max_quote_age}"
            )

    async def close(self) -> None:
        """释放底层 Provider 持有的资源。"""

        await self._provider.close()
