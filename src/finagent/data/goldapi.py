"""GoldAPI 国际黄金人民币克价适配器。

本模块把 GoldAPI 的 ``XAU/CNY`` HTTP JSON 响应转换为项目统一的 ``Quote``。GoldAPI
返回的 ``price`` 以金衡盎司计价，而用户在京东积存金中使用“克”作为持仓单位，因此本
适配器明确读取 ``price_gram_24k``，并使用 ``XAU-CNY-GRAM`` 作为资产代码，防止单位
混用造成约 31.1 倍的估值错误。

这里得到的是国际 24K 黄金现货人民币参考价，不是浙商银行积存金的可成交卖出价。后续
业务层只能用它做行情观察和价差比较；实际卖出到账仍必须使用用户手工录入的京东卖出价，
再扣除相应手续费。
"""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from finagent.core.config import Settings
from finagent.data.base import normalize_symbol
from finagent.data.cache import QuoteCache
from finagent.data.errors import (
    MarketDataAuthenticationError,
    MarketDataClosedError,
    MarketDataConnectionError,
    MarketDataNotFoundError,
    MarketDataRateLimitError,
    MarketDataResponseError,
    MarketDataTimeoutError,
)
from finagent.portfolio import Currency, Quote

GOLD_REFERENCE_SYMBOL = "XAU-CNY-GRAM"

_GOLDAPI_METAL = "XAU"
_GOLDAPI_CURRENCY = "CNY"
_GOLDAPI_PATH = f"/{_GOLDAPI_METAL}/{_GOLDAPI_CURRENCY}"
_SOURCE_NAME = "GoldAPI XAU/CNY 24K 国际现货参考价（人民币/克）"


def _required_field(payload: dict[str, Any], field_name: str) -> Any:
    """读取必需响应字段，并为字段缺失提供稳定、可理解的领域异常。

    Args:
        payload: 已确认根节点为 JSON object 的 GoldAPI 响应。
        field_name: 必须存在的字段名。

    Returns:
        字段原始值；具体类型和取值由后续解析函数继续校验。

    Raises:
        MarketDataResponseError: 响应中不存在该字段。
    """

    if field_name not in payload:
        raise MarketDataResponseError(f"GoldAPI 响应缺少字段：{field_name}")
    return payload[field_name]


def _parse_positive_decimal(value: Any, *, field_name: str) -> Decimal:
    """把外部价格转换为有限正 ``Decimal``，拒绝布尔值和二进制浮点数。

    ``json.loads`` 在本模块中已经通过 ``parse_float=Decimal`` 保留 JSON 小数精度；如果测试
    或未来其他调用路径直接传入 Python float，则明确拒绝，避免把二进制误差带入金融模型。
    """

    if isinstance(value, (bool, float)):
        raise MarketDataResponseError(f"GoldAPI 字段 {field_name} 不是精确金融数值")

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MarketDataResponseError(f"GoldAPI 字段 {field_name} 不是有效数值") from error

    if not parsed.is_finite() or parsed <= 0:
        raise MarketDataResponseError(f"GoldAPI 字段 {field_name} 必须是有限正数")
    return parsed


def _parse_timestamp(value: Any) -> datetime:
    """把 GoldAPI Unix 秒级时间戳转换为带 UTC 时区的 ``datetime``。

    GoldAPI 官方响应使用 Unix timestamp。这里不接受浮点时间戳，避免悄悄截断小数，也不
    把毫秒时间戳猜测成秒；供应商契约发生变化时应立即暴露，而不是生成错误日期。
    """

    if isinstance(value, bool):
        raise MarketDataResponseError("GoldAPI timestamp 不能是布尔值")

    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise MarketDataResponseError("GoldAPI timestamp 不是有效 Unix 秒级时间戳") from error

    if isinstance(value, float) or str(timestamp) != str(value).strip():
        raise MarketDataResponseError("GoldAPI timestamp 必须是整数 Unix 秒级时间戳")
    if timestamp <= 0:
        raise MarketDataResponseError("GoldAPI timestamp 必须大于 0")

    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OSError, OverflowError, ValueError) as error:
        raise MarketDataResponseError("GoldAPI timestamp 超出系统可表示范围") from error


def _parse_quote(payload: Any) -> Quote:
    """校验 GoldAPI 响应语义并生成统一人民币克价 ``Quote``。

    除了检查字段是否存在，还会核对返回的金属和币种。即使 HTTP 请求成功，如果代理、假
    服务或上游错误返回了 XAG/USD，也不能把该价格误配给 XAU/CNY。
    """

    if not isinstance(payload, dict):
        raise MarketDataResponseError("GoldAPI JSON 根节点必须是 object")

    metal = _required_field(payload, "metal")
    currency = _required_field(payload, "currency")
    if not isinstance(metal, str) or metal.upper() != _GOLDAPI_METAL:
        raise MarketDataResponseError("GoldAPI 返回的 metal 与请求的 XAU 不匹配")
    if not isinstance(currency, str) or currency.upper() != _GOLDAPI_CURRENCY:
        raise MarketDataResponseError("GoldAPI 返回的 currency 与请求的 CNY 不匹配")

    price_per_gram = _parse_positive_decimal(
        _required_field(payload, "price_gram_24k"),
        field_name="price_gram_24k",
    )
    as_of = _parse_timestamp(_required_field(payload, "timestamp"))

    return Quote(
        symbol=GOLD_REFERENCE_SYMBOL,
        price=price_per_gram,
        currency=Currency.CNY,
        as_of=as_of,
        source=_SOURCE_NAME,
        # GoldAPI 把该端点定义为实时现货数据。是否已经过时仍由 MarketDataService 根据
        # timestamp 和业务阈值判断，Provider 不因主观猜测直接改写供应商时间语义。
        is_delayed=False,
    )


class GoldApiMarketDataProvider:
    """通过 GoldAPI 查询国际黄金 24K 人民币克价参考行情。

    Args:
        settings: 包含 GoldAPI 密钥、基础地址、超时和缓存 TTL 的只读应用配置。
        client: 可选异步 HTTP 客户端。测试可注入 ``MockTransport`` 客户端；省略时由
            Provider 创建并负责关闭。
        cache: 可选统一行情缓存；省略时按配置创建进程内 TTL 缓存。

    Important:
        Provider 每次缓存未命中只发出一次请求，不隐藏自动重试。是否重试需要由更高层依据
        超时、限流或服务端错误类型以及幂等性作出明确决策，避免免费额度被悄悄消耗。
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        cache: QuoteCache | None = None,
    ) -> None:
        # 只有真正创建 GoldAPI Provider 时才要求密钥存在；基金查询和离线功能不受影响。
        self._api_key = settings.require_goldapi_api_key()
        self._base_url = str(settings.goldapi_base_url).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.goldapi_timeout_seconds)
        self._cache = cache or QuoteCache(settings.goldapi_cache_ttl_seconds)
        self._closed = False

    async def get_quote(self, symbol: str) -> Quote:
        """查询 ``XAU-CNY-GRAM`` 国际黄金人民币克价。

        Args:
            symbol: 目前只支持 ``XAU-CNY-GRAM``，用于把“金衡盎司”和“克”的单位差异写进
                资产标识，避免与普通 ``XAU/CNY`` 报价混淆。

        Returns:
            价格单位为人民币/克、时间为 GoldAPI 原始时间戳的统一行情。

        Raises:
            MarketDataClosedError: Provider 已关闭。
            ValueError: 请求了当前 Provider 不支持的资产代码。
            MarketDataAuthenticationError: API Key 无效或没有访问权限。
            MarketDataRateLimitError: 请求频率或月度额度受限。
            MarketDataTimeoutError: HTTP 请求超时。
            MarketDataConnectionError: DNS、代理、TLS 或连接建立失败。
            MarketDataNotFoundError: GoldAPI 不存在请求的行情。
            MarketDataResponseError: HTTP 状态或 JSON 内容不符合约定。
        """

        if self._closed:
            raise MarketDataClosedError("GoldApiMarketDataProvider 已关闭")

        normalized_symbol = normalize_symbol(symbol)
        if normalized_symbol != GOLD_REFERENCE_SYMBOL:
            raise ValueError(f"GoldAPI Provider 暂不支持资产代码：{normalized_symbol}")

        cached_quote = self._cache.get(normalized_symbol)
        if cached_quote is not None:
            return cached_quote

        try:
            response = await self._client.get(
                f"{self._base_url}{_GOLDAPI_PATH}",
                headers={
                    # API Key 只进入认证请求头，绝不能写入 URL、日志或异常消息。
                    "x-access-token": self._api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as error:
            raise MarketDataTimeoutError("GoldAPI 黄金行情请求超时") from error
        except httpx.TransportError as error:
            raise MarketDataConnectionError("无法连接 GoldAPI，请检查网络、代理和 TLS") from error

        self._raise_for_status(response.status_code)

        try:
            # parse_float=Decimal 让 JSON 小数直接进入十进制定点类型，避免先变成 float 后
            # 再转换时携带二进制误差。
            payload = json.loads(response.content, parse_float=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MarketDataResponseError("GoldAPI 返回的内容不是合法 JSON") from error

        quote = _parse_quote(payload)
        self._cache.put(quote)
        return quote

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        """把 HTTP 状态码转换为稳定领域异常，不泄露上游响应正文。

        响应正文可能包含供应商内部信息，也可能被代理替换为大段 HTML，因此异常只保留状态
        语义。重试策略将来可以依赖这些稳定异常，而不用认识 httpx 或 GoldAPI 的错误对象。
        """

        if 200 <= status_code < 300:
            return
        if status_code in {401, 403}:
            raise MarketDataAuthenticationError("GoldAPI 鉴权失败，请检查 API Key 和套餐权限")
        if status_code == 404:
            raise MarketDataNotFoundError("GoldAPI 中不存在 XAU/CNY 黄金行情")
        if status_code == 429:
            raise MarketDataRateLimitError("GoldAPI 请求频率或账户额度已达到限制")
        if status_code >= 500:
            raise MarketDataResponseError(f"GoldAPI 服务端暂时不可用（HTTP {status_code}）")
        raise MarketDataResponseError(f"GoldAPI 拒绝了行情请求（HTTP {status_code}）")

    async def close(self) -> None:
        """关闭自建 HTTP 客户端并清空缓存；重复调用保持幂等。

        外部注入的客户端可能被其他 Provider 共享，所以本类只关闭自己创建的客户端。
        """

        if self._closed:
            return
        self._closed = True
        self._cache.clear()
        if self._owns_client:
            await self._client.aclose()
