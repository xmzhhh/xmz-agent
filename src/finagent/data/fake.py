"""用于开发、演示和单元测试的内存行情 Provider。

假 Provider 不访问网络，构造时注入的行情就是全部数据源。它还可以模拟延迟、缺失和
关闭状态，使上层服务在不依赖真实网站稳定性的情况下覆盖失败路径。
"""

import asyncio
import math
from collections.abc import Sequence

from finagent.data.base import normalize_symbol
from finagent.data.errors import (
    MarketDataClosedError,
    MarketDataNotFoundError,
    MarketDataResponseError,
)
from finagent.portfolio import Quote


class FakeMarketDataProvider:
    """从内存字典返回确定性行情的异步 Provider。

    Args:
        quotes: 初始行情集合，同一规范化代码不能重复。
        latency_seconds: 每次请求前模拟等待的秒数，用于测试应用层超时。
    """

    def __init__(
        self,
        quotes: Sequence[Quote],
        *,
        latency_seconds: float = 0,
    ) -> None:
        if not math.isfinite(latency_seconds) or latency_seconds < 0:
            raise ValueError("latency_seconds 必须是有限的非负数")

        self._quotes: dict[str, Quote] = {}
        for quote in quotes:
            if quote.symbol in self._quotes:
                raise MarketDataResponseError(f"假行情代码重复：{quote.symbol}")
            self._quotes[quote.symbol] = quote

        self._latency_seconds = latency_seconds
        self._requested_symbols: list[str] = []
        self._closed = False

    @property
    def requested_symbols(self) -> tuple[str, ...]:
        """返回只读请求轨迹，便于测试和演示确认实际访问顺序。"""

        return tuple(self._requested_symbols)

    async def get_quote(self, symbol: str) -> Quote:
        """返回指定代码行情，或给出稳定的缺失/关闭异常。"""

        if self._closed:
            raise MarketDataClosedError("FakeMarketDataProvider 已关闭")

        normalized_symbol = normalize_symbol(symbol)
        self._requested_symbols.append(normalized_symbol)
        if self._latency_seconds:
            await asyncio.sleep(self._latency_seconds)

        try:
            return self._quotes[normalized_symbol]
        except KeyError as error:
            raise MarketDataNotFoundError(f"假行情中不存在资产：{normalized_symbol}") from error

    async def close(self) -> None:
        """把 Provider 标记为关闭；重复关闭保持幂等。"""

        self._closed = True
