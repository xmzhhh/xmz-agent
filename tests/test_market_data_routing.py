"""多数据源行情路由的完全离线测试。

这些测试使用 Fake Provider 验证路由选择、异常边界、批量调用和资源关闭，不访问 AKShare
或 GoldAPI。请求轨迹不仅检查最终结果，还证明未被选中的 Provider 没有收到错误请求。
"""

from datetime import UTC, datetime

import pytest

from finagent.data import (
    GOLD_REFERENCE_SYMBOL,
    FakeMarketDataProvider,
    MarketDataClosedError,
    MarketDataProvider,
    MarketDataService,
    MarketDataTimeoutError,
    RoutingMarketDataProvider,
    UnsupportedMarketDataSymbolError,
)
from finagent.portfolio import Currency, Quote

NOW = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
FUND_SYMBOL = "017811"


def make_quote(symbol: str, *, price: str, source: str) -> Quote:
    """构造一条可精确比较的测试行情。"""

    return Quote.model_validate(
        {
            "symbol": symbol,
            "price": price,
            "currency": Currency.CNY,
            "as_of": NOW,
            "source": source,
            "is_delayed": symbol == FUND_SYMBOL,
        }
    )


def make_router() -> tuple[
    RoutingMarketDataProvider,
    FakeMarketDataProvider,
    FakeMarketDataProvider,
    Quote,
    Quote,
]:
    """创建包含独立基金和黄金 Fake Provider 的标准路由测试夹具。"""

    fund_quote = make_quote(FUND_SYMBOL, price="3.9179", source="基金测试数据")
    gold_quote = make_quote(
        GOLD_REFERENCE_SYMBOL,
        price="877.49",
        source="黄金测试数据",
    )
    fund_provider = FakeMarketDataProvider([fund_quote])
    gold_provider = FakeMarketDataProvider([gold_quote])
    router = RoutingMarketDataProvider(
        fund_provider=fund_provider,
        fund_symbols={FUND_SYMBOL},
        gold_provider=gold_provider,
    )
    return router, fund_provider, gold_provider, fund_quote, gold_quote


class TimeoutProvider:
    """始终抛出同一个超时异常，用于证明 Router 不会改写子 Provider 异常。"""

    def __init__(self, error: MarketDataTimeoutError) -> None:
        self._error = error
        self.closed = False

    async def get_quote(self, symbol: str) -> Quote:
        """模拟已选中数据源在请求阶段超时。"""

        raise self._error

    async def close(self) -> None:
        """记录资源已释放，重复关闭仍然安全。"""

        self.closed = True


class CloseCountingProvider:
    """记录关闭次数，用于验证 Router 自身的幂等关闭保护。"""

    def __init__(self, quote: Quote) -> None:
        self._provider = FakeMarketDataProvider([quote])
        self.close_calls = 0

    async def get_quote(self, symbol: str) -> Quote:
        """把查询交给内部 Fake Provider。"""

        return await self._provider.get_quote(symbol)

    async def close(self) -> None:
        """记录调用次数并关闭内部 Fake Provider。"""

        self.close_calls += 1
        await self._provider.close()


def test_router_satisfies_runtime_provider_protocol() -> None:
    """组合路由器应能被上层当作普通 MarketDataProvider 使用。"""

    router, _, _, _, _ = make_router()

    assert isinstance(router, MarketDataProvider)


@pytest.mark.parametrize("symbol", ["12345", "ABCDEF", "FUND.017811"])
def test_router_rejects_invalid_configured_fund_symbol(symbol: str) -> None:
    """基金白名单只接受六位数字，配置错误应在启动阶段暴露。"""

    with pytest.raises(ValueError, match="六位数字"):
        RoutingMarketDataProvider(
            fund_provider=FakeMarketDataProvider([]),
            fund_symbols={symbol},
            gold_provider=FakeMarketDataProvider([]),
        )


@pytest.mark.asyncio
async def test_router_sends_configured_fund_only_to_fund_provider() -> None:
    """基金路由命中后不得误调用黄金 Provider，且 Quote 必须原样返回。"""

    router, fund_provider, gold_provider, fund_quote, _ = make_router()

    result = await router.get_quote(" 017811 ")

    assert result is fund_quote
    assert fund_provider.requested_symbols == (FUND_SYMBOL,)
    assert gold_provider.requested_symbols == ()


@pytest.mark.asyncio
async def test_router_sends_gold_only_to_gold_provider() -> None:
    """黄金代码支持大小写规范化，但基金 Provider 不应收到该请求。"""

    router, fund_provider, gold_provider, _, gold_quote = make_router()

    result = await router.get_quote(" xau-cny-gram ")

    assert result is gold_quote
    assert fund_provider.requested_symbols == ()
    assert gold_provider.requested_symbols == (GOLD_REFERENCE_SYMBOL,)


@pytest.mark.asyncio
async def test_router_rejects_unconfigured_six_digit_symbol_before_provider_call() -> None:
    """六位数字不等于基金；未进入白名单的代码必须在外部请求前失败。"""

    router, fund_provider, gold_provider, _, _ = make_router()

    with pytest.raises(UnsupportedMarketDataSymbolError, match="000001"):
        await router.get_quote("000001")

    assert fund_provider.requested_symbols == ()
    assert gold_provider.requested_symbols == ()


@pytest.mark.asyncio
async def test_router_preserves_selected_provider_exception_object() -> None:
    """正确路由后的超时不是“不支持代码”，Router 必须原样传播异常。"""

    expected_error = MarketDataTimeoutError("AKShare 测试超时")
    fund_provider = TimeoutProvider(expected_error)
    router = RoutingMarketDataProvider(
        fund_provider=fund_provider,
        fund_symbols={FUND_SYMBOL},
        gold_provider=FakeMarketDataProvider([]),
    )

    with pytest.raises(MarketDataTimeoutError) as captured:
        await router.get_quote(FUND_SYMBOL)

    assert captured.value is expected_error


@pytest.mark.asyncio
async def test_service_fetches_fund_and_gold_through_one_router_in_order() -> None:
    """一个 Service 应能保持输入顺序，经同一 Router 查询两个独立数据源。"""

    router, fund_provider, gold_provider, fund_quote, gold_quote = make_router()
    service = MarketDataService(router)

    results = await service.get_quotes([FUND_SYMBOL, GOLD_REFERENCE_SYMBOL])

    assert results == (fund_quote, gold_quote)
    assert fund_provider.requested_symbols == (FUND_SYMBOL,)
    assert gold_provider.requested_symbols == (GOLD_REFERENCE_SYMBOL,)


@pytest.mark.asyncio
async def test_service_stops_batch_after_first_routed_provider_failure() -> None:
    """当前批量策略遇错即停，基金失败后不应继续请求黄金。"""

    expected_error = MarketDataTimeoutError("基金查询失败")
    gold_provider = FakeMarketDataProvider(
        [make_quote(GOLD_REFERENCE_SYMBOL, price="877.49", source="黄金测试数据")]
    )
    router = RoutingMarketDataProvider(
        fund_provider=TimeoutProvider(expected_error),
        fund_symbols={FUND_SYMBOL},
        gold_provider=gold_provider,
    )
    service = MarketDataService(router)

    with pytest.raises(MarketDataTimeoutError):
        await service.get_quotes([FUND_SYMBOL, GOLD_REFERENCE_SYMBOL])

    assert gold_provider.requested_symbols == ()


@pytest.mark.asyncio
async def test_router_close_closes_each_child_once_and_rejects_new_requests() -> None:
    """Router 应接管子 Provider 生命周期，重复关闭不能重复释放资源。"""

    fund_provider = CloseCountingProvider(
        make_quote(FUND_SYMBOL, price="3.9179", source="基金测试数据")
    )
    gold_provider = CloseCountingProvider(
        make_quote(GOLD_REFERENCE_SYMBOL, price="877.49", source="黄金测试数据")
    )
    router = RoutingMarketDataProvider(
        fund_provider=fund_provider,
        fund_symbols={FUND_SYMBOL},
        gold_provider=gold_provider,
    )

    await router.close()
    await router.close()

    assert fund_provider.close_calls == 1
    assert gold_provider.close_calls == 1
    with pytest.raises(MarketDataClosedError, match="RoutingMarketDataProvider"):
        await router.get_quote(FUND_SYMBOL)
