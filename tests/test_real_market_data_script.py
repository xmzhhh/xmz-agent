"""真实行情人工验收脚本的离线流程测试。

脚本本身会访问 AKShare 和 GoldAPI，但这些自动测试只注入 ``FakeMarketDataProvider``，验证
输出语义、失败隔离、密钥缺失和资源关闭，不产生任何真实网络请求或 API 额度消耗。
"""

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from finagent.core.config import Settings
from finagent.data import (
    GOLD_REFERENCE_SYMBOL,
    FakeMarketDataProvider,
    MarketDataClosedError,
)
from finagent.data.diagnostics import check_real_market_data
from finagent.portfolio import Currency, Quote


def make_settings(*, include_gold_key: bool = True) -> Settings:
    """创建不读取开发者本机 ``.env`` 的验收脚本测试配置。"""

    return Settings(
        llm_api_key=SecretStr("test-llm-secret"),
        goldapi_api_key=SecretStr("test-gold-secret") if include_gold_key else None,
        _env_file=None,  # type: ignore[call-arg]
    )


def make_quote(
    symbol: str,
    *,
    price: str,
    source: str,
    is_delayed: bool,
) -> Quote:
    """构造已经通过统一领域模型校验的固定行情。"""

    return Quote.model_validate(
        {
            "symbol": symbol,
            "price": price,
            "currency": Currency.CNY,
            "as_of": datetime(2026, 7, 15, 15, 0, tzinfo=UTC),
            "source": source,
            "is_delayed": is_delayed,
        }
    )


@pytest.mark.asyncio
async def test_script_prints_units_business_boundaries_and_no_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """成功输出必须说明份/克单位和价格边界，同时不得泄露测试密钥。"""

    fund_provider = FakeMarketDataProvider(
        [
            make_quote(
                "017811",
                price="3.9988",
                source="AKShare 测试净值",
                is_delayed=True,
            )
        ]
    )
    gold_provider = FakeMarketDataProvider(
        [
            make_quote(
                GOLD_REFERENCE_SYMBOL,
                price="937.4573",
                source="GoldAPI 测试参考价",
                is_delayed=False,
            )
        ]
    )

    succeeded = await check_real_market_data(
        make_settings(),
        fund_provider=fund_provider,
        gold_provider=gold_provider,
    )

    output = capsys.readouterr().out
    assert succeeded is True
    assert "3.9988 人民币/份" in output
    assert "937.4573 人民币/克" in output
    assert "不是盘中实时成交价" in output
    assert "不是京东积存金可成交卖出价" in output
    assert "全部成功：是" in output
    assert "test-llm-secret" not in output
    assert "test-gold-secret" not in output


@pytest.mark.asyncio
async def test_fund_failure_does_not_hide_successful_gold_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """基金数据缺失时仍应继续检查黄金，并在汇总中报告整体未通过。"""

    fund_provider = FakeMarketDataProvider([])
    gold_provider = FakeMarketDataProvider(
        [
            make_quote(
                GOLD_REFERENCE_SYMBOL,
                price="938.10",
                source="GoldAPI 测试参考价",
                is_delayed=False,
            )
        ]
    )

    succeeded = await check_real_market_data(
        make_settings(),
        fund_provider=fund_provider,
        gold_provider=gold_provider,
    )

    output = capsys.readouterr().out
    assert succeeded is False
    assert "[失败] AKShare 基金 017811" in output
    assert "[成功] GoldAPI 国际黄金人民币克价" in output
    assert gold_provider.requested_symbols == (GOLD_REFERENCE_SYMBOL,)
    assert "全部成功：否" in output


@pytest.mark.asyncio
async def test_missing_goldapi_key_is_reported_after_fund_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GoldAPI 密钥缺失不应阻止基金查询，但整体验收必须返回失败。"""

    fund_provider = FakeMarketDataProvider(
        [
            make_quote(
                "017811",
                price="3.9988",
                source="AKShare 测试净值",
                is_delayed=True,
            )
        ]
    )

    succeeded = await check_real_market_data(
        make_settings(include_gold_key=False),
        fund_provider=fund_provider,
    )

    output = capsys.readouterr().out
    assert succeeded is False
    assert "[成功] AKShare 基金 017811" in output
    assert "[失败] GoldAPI 国际黄金人民币克价" in output
    assert "缺少 GOLDAPI_API_KEY" in output


@pytest.mark.asyncio
async def test_script_closes_injected_providers() -> None:
    """人工检查结束后，两个 Provider 都必须进入关闭状态。"""

    fund_provider = FakeMarketDataProvider(
        [make_quote("017811", price="3.9988", source="基金", is_delayed=True)]
    )
    gold_provider = FakeMarketDataProvider(
        [
            make_quote(
                GOLD_REFERENCE_SYMBOL,
                price="937.45",
                source="黄金",
                is_delayed=False,
            )
        ]
    )

    await check_real_market_data(
        make_settings(),
        fund_provider=fund_provider,
        gold_provider=gold_provider,
    )

    with pytest.raises(MarketDataClosedError):
        await fund_provider.get_quote("017811")
    with pytest.raises(MarketDataClosedError):
        await gold_provider.get_quote(GOLD_REFERENCE_SYMBOL)


@pytest.mark.asyncio
async def test_script_requires_at_least_one_selected_source() -> None:
    """同时关闭两个检查项属于调用错误，不能输出虚假的“全部成功”。"""

    with pytest.raises(ValueError, match="至少需要选择一个"):
        await check_real_market_data(
            make_settings(),
            include_fund=False,
            include_gold=False,
        )
