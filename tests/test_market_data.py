"""市场数据协议、假 Provider 和应用服务的离线测试。

测试不访问任何真实行情网站，重点验证供应商可替换性、代码规范化、批量顺序、超时、
陈旧行情、响应错配和资源关闭等真实适配器必须遵守的契约。
"""

from datetime import UTC, datetime, timedelta

import pytest

from finagent.data import (
    DuplicateSymbolRequestError,
    FakeMarketDataProvider,
    MarketDataClosedError,
    MarketDataNotFoundError,
    MarketDataProvider,
    MarketDataResponseError,
    MarketDataService,
    MarketDataTimeoutError,
    StaleQuoteError,
    normalize_symbol,
)
from finagent.portfolio import Currency, Quote

NOW = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)


def make_quote(
    symbol: str = "AAA",
    *,
    price: str = "100",
    as_of: datetime = NOW,
) -> Quote:
    """构造可复用的合法固定行情。"""

    return Quote.model_validate(
        {
            "symbol": symbol,
            "price": price,
            "currency": Currency.CNY,
            "as_of": as_of,
            "source": "Fake Provider 测试数据",
            "is_delayed": True,
        }
    )


class WrongSymbolProvider:
    """故意返回错误资产代码，用于验证应用层响应契约。"""

    async def get_quote(self, symbol: str) -> Quote:
        """无论请求什么都返回 BBB。"""

        return make_quote("BBB")

    async def close(self) -> None:
        """测试对象没有真实资源。"""


def test_fake_provider_satisfies_runtime_protocol() -> None:
    """假实现应满足与未来真实适配器相同的结构化协议。"""

    provider = FakeMarketDataProvider([make_quote()])

    assert isinstance(provider, MarketDataProvider)


@pytest.mark.parametrize(
    ("raw_symbol", "expected"),
    [(" aaa ", "AAA"), ("fund.001", "FUND.001"), ("518880", "518880")],
)
def test_normalize_symbol_strips_and_uppercases(raw_symbol: str, expected: str) -> None:
    """不同入口的资产代码应先转换成稳定、可比较的形式。"""

    assert normalize_symbol(raw_symbol) == expected


@pytest.mark.parametrize("symbol", ["", "   ", "A/B", "A B"])
def test_normalize_symbol_rejects_empty_or_unsupported_code(symbol: str) -> None:
    """空代码和当前不支持的字符必须在访问 Provider 前被拒绝。"""

    with pytest.raises(ValueError, match="不能为空|不支持"):
        normalize_symbol(symbol)


@pytest.mark.asyncio
async def test_fake_provider_returns_quote_and_records_normalized_request() -> None:
    """假 Provider 应支持大小写输入，并留下可验证的请求轨迹。"""

    provider = FakeMarketDataProvider([make_quote("AAA")])

    result = await provider.get_quote(" aaa ")

    assert result.symbol == "AAA"
    assert provider.requested_symbols == ("AAA",)


@pytest.mark.asyncio
async def test_fake_provider_reports_missing_symbol() -> None:
    """不存在的资产应返回市场数据异常，而不是原始 KeyError。"""

    provider = FakeMarketDataProvider([])

    with pytest.raises(MarketDataNotFoundError, match="MISSING"):
        await provider.get_quote("MISSING")


def test_fake_provider_rejects_duplicate_initial_quotes() -> None:
    """两条同代码假行情存在歧义，构造 Provider 时就应失败。"""

    with pytest.raises(MarketDataResponseError, match="代码重复"):
        FakeMarketDataProvider([make_quote(), make_quote(price="101")])


@pytest.mark.parametrize("latency", [-1, float("inf"), float("nan")])
def test_fake_provider_rejects_invalid_latency(latency: float) -> None:
    """负数和非有限延迟无法形成有效异步模拟。"""

    with pytest.raises(ValueError, match="latency_seconds"):
        FakeMarketDataProvider([], latency_seconds=latency)


@pytest.mark.asyncio
async def test_closed_fake_provider_rejects_new_requests() -> None:
    """关闭后的 Provider 不应继续提供数据，避免资源生命周期混乱。"""

    provider = FakeMarketDataProvider([make_quote()])
    await provider.close()

    with pytest.raises(MarketDataClosedError, match="已关闭"):
        await provider.get_quote("AAA")


@pytest.mark.asyncio
async def test_service_preserves_batch_request_order() -> None:
    """批量结果顺序必须与请求顺序一致，便于上层稳定匹配持仓。"""

    provider = FakeMarketDataProvider([make_quote("AAA"), make_quote("BBB")])
    service = MarketDataService(provider)

    results = await service.get_quotes(["bbb", "aaa"])

    assert [quote.symbol for quote in results] == ["BBB", "AAA"]
    assert provider.requested_symbols == ("BBB", "AAA")


@pytest.mark.asyncio
async def test_service_rejects_duplicate_normalized_symbols() -> None:
    """AAA 与小写 aaa 规范化后重复，不能浪费两次行情请求。"""

    provider = FakeMarketDataProvider([make_quote("AAA")])
    service = MarketDataService(provider)

    with pytest.raises(DuplicateSymbolRequestError, match="重复"):
        await service.get_quotes(["AAA", " aaa "])

    assert provider.requested_symbols == ()


@pytest.mark.asyncio
async def test_service_accepts_empty_batch_without_provider_call() -> None:
    """空持仓对应空行情批次，应直接返回空元组。"""

    provider = FakeMarketDataProvider([])
    service = MarketDataService(provider)

    assert await service.get_quotes([]) == ()
    assert provider.requested_symbols == ()


@pytest.mark.asyncio
async def test_service_converts_async_timeout_to_domain_error() -> None:
    """慢 Provider 应被应用级超时取消，并转换为稳定市场数据异常。"""

    provider = FakeMarketDataProvider([make_quote()], latency_seconds=0.05)
    service = MarketDataService(provider, request_timeout_seconds=0.001)

    with pytest.raises(MarketDataTimeoutError, match="AAA"):
        await service.get_quote("AAA")


@pytest.mark.asyncio
async def test_service_rejects_mismatched_provider_response() -> None:
    """请求 AAA 却返回 BBB 属于供应商契约错误，不能交给组合计算器。"""

    service = MarketDataService(WrongSymbolProvider())

    with pytest.raises(MarketDataResponseError, match="返回 BBB"):
        await service.get_quote("AAA")


@pytest.mark.asyncio
async def test_service_rejects_stale_quote() -> None:
    """早于最大允许年龄的行情不能被表述为当前市场事实。"""

    old_quote = make_quote(as_of=NOW - timedelta(minutes=31))
    service = MarketDataService(
        FakeMarketDataProvider([old_quote]),
        max_quote_age=timedelta(minutes=30),
        clock=lambda: NOW,
    )

    with pytest.raises(StaleQuoteError, match="已陈旧"):
        await service.get_quote("AAA")


@pytest.mark.asyncio
async def test_service_accepts_quote_at_freshness_boundary() -> None:
    """恰好等于最大年龄的行情仍然有效，只有严格超过阈值才拒绝。"""

    boundary_quote = make_quote(as_of=NOW - timedelta(minutes=30))
    service = MarketDataService(
        FakeMarketDataProvider([boundary_quote]),
        max_quote_age=timedelta(minutes=30),
        clock=lambda: NOW,
    )

    assert await service.get_quote("AAA") == boundary_quote


@pytest.mark.asyncio
async def test_service_rejects_naive_application_clock() -> None:
    """测试或生产时钟缺少时区会破坏行情年龄计算，应明确报错。"""

    service = MarketDataService(
        FakeMarketDataProvider([make_quote()]),
        max_quote_age=timedelta(minutes=30),
        clock=lambda: datetime(2026, 7, 14, 10, 0),
    )

    with pytest.raises(MarketDataResponseError, match="clock 必须返回带时区"):
        await service.get_quote("AAA")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_service_rejects_invalid_timeout(timeout: float) -> None:
    """非正数或非有限超时会让保护逻辑失去明确语义。"""

    with pytest.raises(ValueError, match="request_timeout_seconds"):
        MarketDataService(FakeMarketDataProvider([]), request_timeout_seconds=timeout)


def test_service_rejects_negative_max_quote_age() -> None:
    """行情最大年龄不能是负时间。"""

    with pytest.raises(ValueError, match="max_quote_age"):
        MarketDataService(
            FakeMarketDataProvider([]),
            max_quote_age=timedelta(seconds=-1),
        )


@pytest.mark.asyncio
async def test_service_close_delegates_to_provider() -> None:
    """关闭服务必须释放底层 Provider，防止真实 HTTP 连接泄漏。"""

    provider = FakeMarketDataProvider([make_quote()])
    service = MarketDataService(provider)

    await service.close()

    with pytest.raises(MarketDataClosedError):
        await provider.get_quote("AAA")
