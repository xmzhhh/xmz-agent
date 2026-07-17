"""市场行情的轻量进程内缓存。

本模块只缓存已经通过 Pydantic 校验的统一 ``Quote``，不接触 AKShare DataFrame 或
GoldAPI 原始 JSON。这样缓存不会把供应商格式泄漏到业务层，也不会绕过领域模型校验。

第一版使用进程内 TTL 缓存，目标是避免用户连续点击刷新时重复消耗免费 API 额度；它不
负责跨进程持久化。后续进入持久化阶段时，可以在保持 ``get``/``put`` 语义的前提下增加
SQLite 或 Redis 适配器。
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from finagent.data.base import normalize_symbol
from finagent.portfolio import Quote

type MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """缓存内部条目；过期时刻使用单调时钟秒数表示。"""

    quote: Quote
    expires_at: float


class QuoteCache:
    """保存统一行情并在 TTL 到期后自动失效。

    使用 ``time.monotonic`` 而不是系统日期时间，是因为用户修改系统时间、网络校时或夏令
    时切换都不应该让缓存突然延长或提前过期。单调时钟只会向前推进，更适合测量持续时间。

    Args:
        ttl_seconds: 每条行情从写入开始可以复用的秒数，必须是有限正数。
        clock: 返回单调秒数的函数；测试可注入可控时钟，避免真实等待。

    Raises:
        ValueError: TTL 不是有限正数时抛出。
    """

    def __init__(
        self,
        ttl_seconds: float,
        *,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须是有限的正数")

        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, symbol: str) -> Quote | None:
        """读取尚未过期的行情；未命中或过期时返回 ``None``。

        Args:
            symbol: 待查询资产代码，读取前会执行统一规范化。

        Returns:
            有效缓存行情；不存在或恰好到达过期边界时返回 ``None``。
        """

        normalized_symbol = normalize_symbol(symbol)
        entry = self._entries.get(normalized_symbol)
        if entry is None:
            return None

        # 到达过期时刻即视为失效，同时删除条目，防止长期进程积累无用对象。
        if self._clock() >= entry.expires_at:
            del self._entries[normalized_symbol]
            return None
        return entry.quote

    def put(self, quote: Quote) -> None:
        """写入或覆盖一条已经校验的统一行情。

        Args:
            quote: Pydantic 已完成价格、时间、币种和来源校验的行情对象。
        """

        normalized_symbol = normalize_symbol(quote.symbol)
        self._entries[normalized_symbol] = _CacheEntry(
            quote=quote,
            expires_at=self._clock() + self._ttl_seconds,
        )

    def clear(self) -> None:
        """清空全部缓存，供资源关闭、测试隔离或用户强制刷新使用。"""

        self._entries.clear()
