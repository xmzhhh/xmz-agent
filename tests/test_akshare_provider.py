"""AKShare基金净值Provider的离线契约测试。

所有测试都注入本地 DataFrame，不访问东方财富或其他真实网站。测试覆盖最新行选择、异步
线程边界、缓存、字段漂移、非法数据、异常转换和资源关闭，防止上游变化污染组合计算。
"""

import threading
from decimal import Decimal

import pandas as pd
import pytest
from requests import Timeout

from finagent.data import (
    AkShareFundNavProvider,
    MarketDataClosedError,
    MarketDataConnectionError,
    MarketDataNotFoundError,
    MarketDataProvider,
    MarketDataResponseError,
)
from finagent.portfolio import Currency


def make_nav_frame() -> pd.DataFrame:
    """构造故意未排序的净值数据，验证Provider不依赖上游顺序。"""

    return pd.DataFrame(
        {
            "净值日期": ["2026-07-14", "2026-07-11", "2026-07-15"],
            "单位净值": [3.9179, 4.0357, 3.9988],
            "日增长率": [-2.92, -6.39, 2.06],
        }
    )


def test_akshare_provider_satisfies_market_data_protocol() -> None:
    """真实基金适配器应满足与Fake Provider相同的结构化协议。"""

    provider = AkShareFundNavProvider(loader=lambda _symbol: make_nav_frame())

    assert isinstance(provider, MarketDataProvider)


@pytest.mark.asyncio
async def test_provider_returns_latest_confirmed_unit_nav() -> None:
    """应按日期选择最新净值，并明确标记为非盘中实时行情。"""

    provider = AkShareFundNavProvider(loader=lambda _symbol: make_nav_frame())

    quote = await provider.get_quote(" 017811 ")

    assert quote.symbol == "017811"
    assert quote.price == Decimal("3.9988")
    assert quote.currency is Currency.CNY
    assert quote.as_of.isoformat() == "2026-07-15T15:00:00+08:00"
    assert quote.source == "AKShare（东方财富开放式基金净值）"
    assert quote.is_delayed is True


@pytest.mark.asyncio
async def test_provider_runs_sync_loader_outside_event_loop_thread() -> None:
    """同步AKShare调用必须在线程池执行，避免冻结异步Agent事件循环。"""

    event_loop_thread_id = threading.get_ident()
    loader_thread_ids: list[int] = []

    def loader(_symbol: str) -> pd.DataFrame:
        loader_thread_ids.append(threading.get_ident())
        return make_nav_frame()

    provider = AkShareFundNavProvider(loader=loader)

    await provider.get_quote("017811")

    assert loader_thread_ids
    assert loader_thread_ids[0] != event_loop_thread_id


@pytest.mark.asyncio
async def test_provider_reuses_cached_quote() -> None:
    """同一基金的连续查询应命中缓存，避免重复访问上游网站。"""

    requested_symbols: list[str] = []

    def loader(symbol: str) -> pd.DataFrame:
        requested_symbols.append(symbol)
        return make_nav_frame()

    provider = AkShareFundNavProvider(loader=loader)

    first = await provider.get_quote("017811")
    second = await provider.get_quote("017811")

    assert first is second
    assert requested_symbols == ["017811"]


@pytest.mark.parametrize("symbol", ["", "17811", "017811.CN", "ABCDEF"])
@pytest.mark.asyncio
async def test_provider_rejects_invalid_fund_code_without_request(symbol: str) -> None:
    """非法代码应在网络请求前失败，既节省资源也给出更清晰反馈。"""

    requested = False

    def loader(_symbol: str) -> pd.DataFrame:
        nonlocal requested
        requested = True
        return make_nav_frame()

    provider = AkShareFundNavProvider(loader=loader)

    with pytest.raises(ValueError, match="六位数字|不能为空"):
        await provider.get_quote(symbol)
    assert requested is False


@pytest.mark.asyncio
async def test_provider_reports_empty_frame_as_not_found() -> None:
    """空DataFrame表示没有净值，不能伪造0或沿用其他基金数据。"""

    provider = AkShareFundNavProvider(loader=lambda _symbol: pd.DataFrame())

    with pytest.raises(MarketDataNotFoundError, match="017811"):
        await provider.get_quote("017811")


@pytest.mark.asyncio
async def test_provider_reports_missing_columns() -> None:
    """上游字段改名时应明确暴露契约漂移，而不是在计算层随机报错。"""

    frame = pd.DataFrame({"日期": ["2026-07-15"], "净值": [3.9988]})
    provider = AkShareFundNavProvider(loader=lambda _symbol: frame)

    with pytest.raises(MarketDataResponseError, match="净值日期|单位净值"):
        await provider.get_quote("017811")


@pytest.mark.parametrize(
    ("nav_date", "unit_nav"),
    [("不是日期", 3.9988), ("2026-07-15", None), ("2026-07-15", 0)],
)
@pytest.mark.asyncio
async def test_provider_rejects_invalid_nav_values(nav_date: object, unit_nav: object) -> None:
    """非法日期、空净值和非正净值都不能进入统一Quote。"""

    frame = pd.DataFrame({"净值日期": [nav_date], "单位净值": [unit_nav]})
    provider = AkShareFundNavProvider(loader=lambda _symbol: frame)

    with pytest.raises(MarketDataResponseError, match="无效"):
        await provider.get_quote("017811")


@pytest.mark.asyncio
async def test_provider_converts_io_error_to_connection_error() -> None:
    """DNS、连接或文件描述符类错误应转换为稳定连接异常。"""

    def loader(_symbol: str) -> pd.DataFrame:
        raise OSError("测试网络不可用")

    provider = AkShareFundNavProvider(loader=loader)

    with pytest.raises(MarketDataConnectionError, match="017811"):
        await provider.get_quote("017811")


@pytest.mark.asyncio
async def test_provider_converts_requests_timeout_to_connection_error() -> None:
    """AKShare 底层 requests 超时也必须归类为连接故障，而不是响应字段错误。"""

    def loader(_symbol: str) -> pd.DataFrame:
        raise Timeout("测试请求超时")

    provider = AkShareFundNavProvider(loader=loader)

    with pytest.raises(MarketDataConnectionError, match="017811"):
        await provider.get_quote("017811")


@pytest.mark.asyncio
async def test_provider_wraps_unexpected_loader_error() -> None:
    """第三方解析异常不能直接泄漏到上层，而应转成响应异常并保留cause。"""

    original_error = RuntimeError("测试JS解析失败")

    def loader(_symbol: str) -> pd.DataFrame:
        raise original_error

    provider = AkShareFundNavProvider(loader=loader)

    with pytest.raises(MarketDataResponseError, match="017811") as captured:
        await provider.get_quote("017811")
    assert captured.value.__cause__ is original_error


@pytest.mark.asyncio
async def test_closed_provider_rejects_new_requests() -> None:
    """关闭后即使缓存曾经命中也不得继续返回行情。"""

    provider = AkShareFundNavProvider(loader=lambda _symbol: make_nav_frame())
    await provider.get_quote("017811")

    await provider.close()
    await provider.close()

    with pytest.raises(MarketDataClosedError, match="已关闭"):
        await provider.get_quote("017811")
