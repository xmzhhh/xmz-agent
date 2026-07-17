"""离线路由验收脚本的自动测试。

测试直接调用脚本中的异步核心函数并注入 Fake Provider，检查成功输出、错误输出、请求轨迹
和资源关闭。导入脚本不会执行 ``main``，因此 pytest 不会产生隐式副作用或真实网络请求。
"""

from datetime import UTC, datetime

import pytest

import scripts.check_market_data_routing as routing_script
from finagent.data import (
    GOLD_REFERENCE_SYMBOL,
    FakeMarketDataProvider,
    MarketDataClosedError,
)
from finagent.portfolio import Currency, Quote

NOW = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)


def make_quote(
    symbol: str,
    *,
    price: str,
    source: str,
    is_delayed: bool,
) -> Quote:
    """构造脚本测试使用的固定统一行情。"""

    return Quote.model_validate(
        {
            "symbol": symbol,
            "price": price,
            "currency": Currency.CNY,
            "as_of": NOW,
            "source": source,
            "is_delayed": is_delayed,
        }
    )


def make_providers() -> tuple[FakeMarketDataProvider, FakeMarketDataProvider]:
    """创建请求轨迹相互独立的基金与黄金 Fake Provider。"""

    return (
        FakeMarketDataProvider(
            [
                make_quote(
                    routing_script.FUND_SYMBOL,
                    price="3.9988",
                    source="基金脚本测试数据",
                    is_delayed=True,
                )
            ]
        ),
        FakeMarketDataProvider(
            [
                make_quote(
                    GOLD_REFERENCE_SYMBOL,
                    price="938.10",
                    source="黄金脚本测试数据",
                    is_delayed=False,
                )
            ]
        ),
    )


@pytest.mark.asyncio
async def test_script_prints_results_and_exact_provider_traces(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """成功结果必须同时由正确请求轨迹证明，不能只检查两个价格数字。"""

    fund_provider, gold_provider = make_providers()

    succeeded = await routing_script.check_market_data_routing(
        fund_provider=fund_provider,
        gold_provider=gold_provider,
    )

    output = capsys.readouterr().out
    assert succeeded is True
    assert "017811，3.9988 CNY/份" in output
    assert "XAU-CNY-GRAM，938.10 CNY/克" in output
    assert "基金 Provider 实际请求：('017811',)" in output
    assert "黄金 Provider 实际请求：('XAU-CNY-GRAM',)" in output
    assert "真实网络请求：无" in output
    assert "路由验收：通过" in output


@pytest.mark.asyncio
async def test_script_failure_stops_batch_and_still_closes_both_providers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """基金缺失时黄金不再查询，但 finally 仍必须关闭两个子 Provider。"""

    fund_provider = FakeMarketDataProvider([])
    _, gold_provider = make_providers()

    succeeded = await routing_script.check_market_data_routing(
        fund_provider=fund_provider,
        gold_provider=gold_provider,
    )

    output = capsys.readouterr().out
    assert succeeded is False
    assert "错误类型：MarketDataNotFoundError" in output
    assert "路由验收：失败" in output
    assert fund_provider.requested_symbols == (routing_script.FUND_SYMBOL,)
    assert gold_provider.requested_symbols == ()
    with pytest.raises(MarketDataClosedError):
        await fund_provider.get_quote(routing_script.FUND_SYMBOL)
    with pytest.raises(MarketDataClosedError):
        await gold_provider.get_quote(GOLD_REFERENCE_SYMBOL)


def test_main_runs_default_offline_check_without_import_side_effects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """同步入口应能建立事件循环完成默认验收，并以正常退出表示成功。"""

    routing_script.main()

    output = capsys.readouterr().out
    assert "查询顺序：017811 → XAU-CNY-GRAM" in output
    assert "路由验收：通过" in output
