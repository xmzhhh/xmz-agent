"""进程内行情缓存的离线测试。

测试通过可控单调时钟覆盖命中、覆盖、过期边界和清空行为，不进行真实等待，也不访问
AKShare 或 GoldAPI。它防止缓存错误地永久复用旧行情，或因为资产代码大小写而重复请求。
"""

from datetime import UTC, datetime

import pytest

from finagent.data import QuoteCache
from finagent.portfolio import Currency, Quote


class MutableClock:
    """允许测试主动推进秒数的单调时钟。"""

    def __init__(self) -> None:
        self.current = 100.0

    def __call__(self) -> float:
        """返回当前测试秒数。"""

        return self.current


def make_quote(*, price: str = "100") -> Quote:
    """构造一条合法行情，价格使用字符串避免二进制浮点误差。"""

    return Quote.model_validate(
        {
            "symbol": "XAU-CNY-GRAM",
            "price": price,
            "currency": Currency.CNY,
            "as_of": datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
            "source": "缓存测试数据",
            "is_delayed": False,
        }
    )


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
def test_cache_rejects_invalid_ttl(ttl: float) -> None:
    """非正数、非有限值和布尔值都不能形成清晰的缓存持续时间。"""

    with pytest.raises(ValueError, match="ttl_seconds"):
        QuoteCache(ttl)


def test_cache_returns_quote_before_expiration() -> None:
    """TTL内重复读取应命中同一个已校验行情对象。"""

    clock = MutableClock()
    cache = QuoteCache(60, clock=clock)
    quote = make_quote()
    cache.put(quote)

    clock.current += 59.99

    assert cache.get(" xau-cny-gram ") is quote


def test_cache_expires_exactly_at_ttl_boundary() -> None:
    """恰好到达过期时刻必须失效，防止边界上多复用一次旧行情。"""

    clock = MutableClock()
    cache = QuoteCache(60, clock=clock)
    cache.put(make_quote())

    clock.current += 60

    assert cache.get("XAU-CNY-GRAM") is None
    # 过期条目已被删除，后续读取仍应稳定返回未命中。
    assert cache.get("XAU-CNY-GRAM") is None


def test_cache_overwrites_existing_symbol() -> None:
    """同代码的新行情应替换旧行情，并从本次写入重新计算TTL。"""

    clock = MutableClock()
    cache = QuoteCache(60, clock=clock)
    cache.put(make_quote(price="100"))
    clock.current += 30

    latest_quote = make_quote(price="101")
    cache.put(latest_quote)
    clock.current += 31

    assert cache.get("XAU-CNY-GRAM") is latest_quote


def test_cache_clear_removes_all_quotes() -> None:
    """关闭Provider或强制刷新时，clear应立即移除已有行情。"""

    cache = QuoteCache(60)
    cache.put(make_quote())

    cache.clear()

    assert cache.get("XAU-CNY-GRAM") is None
