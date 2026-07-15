"""GoldAPI 国际黄金参考价 Provider 的离线契约测试。

测试使用 ``httpx.MockTransport`` 在内存中模拟 HTTP 服务，不读取开发者本机 ``.env``，也不
消耗 GoldAPI 免费额度。覆盖成功响应、认证头、克价单位、缓存、状态码映射、网络异常、
JSON 契约漂移和资源所有权，防止外部数据未经校验进入投资组合计算。
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr

from finagent.core.config import Settings
from finagent.data import (
    GOLD_REFERENCE_SYMBOL,
    GoldApiMarketDataProvider,
    MarketDataAuthenticationError,
    MarketDataClosedError,
    MarketDataConnectionError,
    MarketDataNotFoundError,
    MarketDataProvider,
    MarketDataRateLimitError,
    MarketDataResponseError,
    MarketDataTimeoutError,
)
from finagent.portfolio import Currency

type MockHandler = Callable[[httpx.Request], httpx.Response]


def make_settings() -> Settings:
    """创建与开发者真实密钥隔离的 GoldAPI 测试配置。"""

    return Settings(
        llm_api_key=SecretStr("test-llm-key"),
        goldapi_api_key=SecretStr("test-gold-key"),
        goldapi_base_url=AnyHttpUrl("https://goldapi.test/api"),
        goldapi_timeout_seconds=3,
        goldapi_cache_ttl_seconds=60,
        _env_file=None,  # type: ignore[call-arg]
    )


def make_payload(**overrides: Any) -> dict[str, Any]:
    """构造最小合法 GoldAPI 响应，并允许单个测试覆盖目标字段。"""

    payload: dict[str, Any] = {
        "timestamp": 1_700_000_000,
        "metal": "XAU",
        "currency": "CNY",
        "price": 29_158.18,
        "price_gram_24k": 937.4573,
    }
    payload.update(overrides)
    return payload


def make_client(handler: MockHandler) -> httpx.AsyncClient:
    """创建使用内存传输层的异步客户端，杜绝测试误访问真实网络。"""

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_goldapi_provider_satisfies_market_data_protocol() -> None:
    """真实黄金适配器应满足与 Fake、AKShare Provider 相同的统一协议。"""

    client = make_client(lambda request: httpx.Response(200, json=make_payload(), request=request))
    provider = GoldApiMarketDataProvider(make_settings(), client=client)

    assert isinstance(provider, MarketDataProvider)


@pytest.mark.asyncio
async def test_provider_requests_xau_cny_and_returns_gram_price() -> None:
    """应携带认证头请求 XAU/CNY，并使用 24K 克价而不是金衡盎司总价。"""

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json=make_payload(), request=request)

    client = make_client(handler)
    provider = GoldApiMarketDataProvider(make_settings(), client=client)

    quote = await provider.get_quote(" xau-cny-gram ")

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert str(request.url) == "https://goldapi.test/api/XAU/CNY"
    assert request.headers["x-access-token"] == "test-gold-key"
    assert quote.symbol == GOLD_REFERENCE_SYMBOL
    assert quote.price == Decimal("937.4573")
    assert quote.price != Decimal("29158.18")
    assert quote.currency is Currency.CNY
    assert quote.as_of.isoformat() == "2023-11-14T22:13:20+00:00"
    assert "人民币/克" in quote.source
    assert quote.is_delayed is False


@pytest.mark.asyncio
async def test_provider_reuses_cached_quote_without_spending_another_request() -> None:
    """相同黄金参考价查询应命中缓存，避免浪费免费 API 月度额度。"""

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=make_payload(), request=request)

    client = make_client(handler)
    provider = GoldApiMarketDataProvider(make_settings(), client=client)

    first = await provider.get_quote(GOLD_REFERENCE_SYMBOL)
    second = await provider.get_quote(GOLD_REFERENCE_SYMBOL)

    assert first is second
    assert request_count == 1


@pytest.mark.asyncio
async def test_provider_rejects_unsupported_symbol_before_http_request() -> None:
    """单位或品种不匹配的代码必须在联网前失败，防止错误行情进入克数持仓。"""

    requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, json=make_payload(), request=request)

    client = make_client(handler)
    provider = GoldApiMarketDataProvider(make_settings(), client=client)

    with pytest.raises(ValueError, match="暂不支持"):
        await provider.get_quote("XAU-CNY")
    assert requested is False


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, MarketDataAuthenticationError),
        (403, MarketDataAuthenticationError),
        (404, MarketDataNotFoundError),
        (429, MarketDataRateLimitError),
        (500, MarketDataResponseError),
        (400, MarketDataResponseError),
    ],
)
@pytest.mark.asyncio
async def test_provider_maps_http_status_to_domain_error(
    status_code: int,
    expected_error: type[Exception],
) -> None:
    """上层重试策略应依赖稳定领域异常，而不需要认识 httpx 状态对象。"""

    client = make_client(
        lambda request: httpx.Response(status_code, text="不应泄露的响应", request=request)
    )
    provider = GoldApiMarketDataProvider(make_settings(), client=client)

    with pytest.raises(expected_error) as captured:
        await provider.get_quote(GOLD_REFERENCE_SYMBOL)
    assert "不应泄露的响应" not in str(captured.value)


@pytest.mark.parametrize(
    ("transport_error", "expected_error"),
    [
        (httpx.ReadTimeout("测试读取超时"), MarketDataTimeoutError),
        (httpx.ConnectError("测试连接失败"), MarketDataConnectionError),
    ],
)
@pytest.mark.asyncio
async def test_provider_maps_transport_error(
    transport_error: httpx.TransportError,
    expected_error: type[Exception],
) -> None:
    """超时和连接失败必须分开分类，为后续有限重试策略保留依据。"""

    def handler(request: httpx.Request) -> httpx.Response:
        transport_error.request = request
        raise transport_error

    client = make_client(handler)
    provider = GoldApiMarketDataProvider(make_settings(), client=client)

    with pytest.raises(expected_error):
        await provider.get_quote(GOLD_REFERENCE_SYMBOL)


@pytest.mark.asyncio
async def test_provider_rejects_invalid_json() -> None:
    """成功状态但响应不是 JSON 时，不得尝试猜测或伪造黄金价格。"""

    client = make_client(
        lambda request: httpx.Response(200, content=b"<html>proxy error</html>", request=request)
    )
    provider = GoldApiMarketDataProvider(make_settings(), client=client)

    with pytest.raises(MarketDataResponseError, match="合法 JSON"):
        await provider.get_quote(GOLD_REFERENCE_SYMBOL)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"metal": "XAU", "currency": "CNY", "timestamp": 1_700_000_000},
        make_payload(metal="XAG"),
        make_payload(currency="USD"),
        make_payload(price_gram_24k=0),
        make_payload(price_gram_24k="NaN"),
        make_payload(timestamp=True),
        make_payload(timestamp=1_700_000_000.5),
    ],
)
@pytest.mark.asyncio
async def test_provider_rejects_invalid_payload(payload: Any) -> None:
    """字段缺失、品种错配和非法金融数值都必须在 Provider 边界失败。"""

    client = make_client(lambda request: httpx.Response(200, json=payload, request=request))
    provider = GoldApiMarketDataProvider(make_settings(), client=client)

    with pytest.raises(MarketDataResponseError):
        await provider.get_quote(GOLD_REFERENCE_SYMBOL)


@pytest.mark.asyncio
async def test_close_does_not_close_injected_client_and_rejects_new_requests() -> None:
    """Provider 只管理自建客户端；关闭后不得再返回缓存或发起网络请求。"""

    client = make_client(lambda request: httpx.Response(200, json=make_payload(), request=request))
    provider = GoldApiMarketDataProvider(make_settings(), client=client)
    await provider.get_quote(GOLD_REFERENCE_SYMBOL)

    await provider.close()
    await provider.close()

    assert client.is_closed is False
    with pytest.raises(MarketDataClosedError, match="已关闭"):
        await provider.get_quote(GOLD_REFERENCE_SYMBOL)
    await client.aclose()
