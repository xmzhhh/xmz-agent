"""AKShare 开放式基金单位净值 Provider。

AKShare 的 ``fund_open_fund_info_em`` 是同步函数，并返回 pandas DataFrame。该格式只允许
存在于本适配器内部：Provider 负责在线程中执行同步调用、检查列和数值，并最终转换为项目
统一的 ``Quote``。业务层和投资组合计算器因此不需要认识 pandas 或东方财富字段。

本 Provider 返回的是“最新已确认单位净值”，不是盘中实时成交价格。``Quote.is_delayed``
始终为 ``True``，避免 Agent 把昨日或当日收盘后确认的基金净值描述成实时行情。
"""

import asyncio
import re
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

import akshare as ak  # type: ignore[import-untyped]
import pandas as pd
from requests import RequestException

from finagent.data.base import normalize_symbol
from finagent.data.cache import QuoteCache
from finagent.data.errors import (
    MarketDataClosedError,
    MarketDataConnectionError,
    MarketDataNotFoundError,
    MarketDataResponseError,
)
from finagent.portfolio import Currency, Quote

type FundNavLoader = Callable[[str], pd.DataFrame]

_FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
_NAV_DATE_COLUMN = "净值日期"
_UNIT_NAV_COLUMN = "单位净值"
_REQUIRED_COLUMNS = frozenset({_NAV_DATE_COLUMN, _UNIT_NAV_COLUMN})
_SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
_FUND_NAV_REFERENCE_TIME = time(hour=15)
_DEFAULT_CACHE_TTL_SECONDS = 3_600.0
_SOURCE_NAME = "AKShare（东方财富开放式基金净值）"


def _load_unit_nav(symbol: str) -> pd.DataFrame:
    """调用 AKShare 获取指定基金成立以来的单位净值走势。

    该函数保持同步形态，便于单独测试和注入替代实现。异步边界由 Provider 的
    ``get_quote`` 使用 ``asyncio.to_thread`` 建立。
    """

    return cast(
        pd.DataFrame,
        ak.fund_open_fund_info_em(
            symbol=symbol,
            indicator="单位净值走势",
            period="成立来",
        ),
    )


def _parse_nav_date(value: object) -> date:
    """把 AKShare 日期值转换为不含时区的基金净值日期。"""

    try:
        # DataFrame 单元格在类型层面是 object；先转成字符串，既覆盖日期字符串、
        # datetime/date/Timestamp，也让 pandas-stubs 能准确选择标量重载。
        parsed = pd.to_datetime(str(value), errors="raise")
    except (TypeError, ValueError) as error:
        raise MarketDataResponseError(f"AKShare 返回无效净值日期：{value!r}") from error

    if not isinstance(parsed, pd.Timestamp) or pd.isna(parsed):
        raise MarketDataResponseError(f"AKShare 返回无效净值日期：{value!r}")
    return parsed.date()


def _parse_unit_nav(value: object) -> Decimal:
    """把单位净值转换为精确 Decimal，并拒绝空值、非有限值和非正数。"""

    try:
        unit_nav = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MarketDataResponseError(f"AKShare 返回无效单位净值：{value!r}") from error

    if not unit_nav.is_finite() or unit_nav <= 0:
        raise MarketDataResponseError(f"AKShare 返回无效单位净值：{value!r}")
    return unit_nav


def _build_latest_quote(symbol: str, frame: pd.DataFrame) -> Quote:
    """校验 DataFrame 并选择净值日期最新的一条记录。

    不依赖 DataFrame 当前排序，因为上游接口可能改成倒序返回。如果任意候选行包含非法
    日期或净值，本阶段选择明确失败，而不是悄悄跳过后返回更旧数据。
    """

    if frame.empty:
        raise MarketDataNotFoundError(f"AKShare 未返回基金 {symbol} 的单位净值")

    missing_columns = _REQUIRED_COLUMNS.difference(str(column) for column in frame.columns)
    if missing_columns:
        missing_text = "、".join(sorted(missing_columns))
        raise MarketDataResponseError(f"AKShare 基金净值响应缺少字段：{missing_text}")

    candidates: list[tuple[date, Decimal]] = []
    selected_columns = frame.loc[:, [_NAV_DATE_COLUMN, _UNIT_NAV_COLUMN]]
    for raw_date, raw_unit_nav in selected_columns.itertuples(index=False, name=None):
        candidates.append((_parse_nav_date(raw_date), _parse_unit_nav(raw_unit_nav)))

    if not candidates:
        raise MarketDataNotFoundError(f"AKShare 未返回基金 {symbol} 的单位净值")

    latest_date, latest_unit_nav = max(candidates, key=lambda item: item[0])

    # 场外基金按交易日净值估值。这里的 15:00 是净值所属交易日的参考时刻，不代表
    # 基金公司恰好在 15:00 发布净值；真实发布时间通常晚于收盘。
    as_of = datetime.combine(
        latest_date,
        _FUND_NAV_REFERENCE_TIME,
        tzinfo=_SHANGHAI_TIMEZONE,
    )
    return Quote(
        symbol=symbol,
        price=latest_unit_nav,
        currency=Currency.CNY,
        as_of=as_of,
        source=_SOURCE_NAME,
        is_delayed=True,
    )


class AkShareFundNavProvider:
    """通过 AKShare 查询中国开放式基金最新已确认单位净值。

    Args:
        loader: 同步数据加载函数。生产环境默认调用 AKShare；测试注入固定 DataFrame。
        cache: 可选统一行情缓存。省略时创建一小时 TTL 的进程内缓存。

    Important:
        ``asyncio.to_thread`` 能避免同步网络请求阻塞事件循环，但取消等待不能强制终止已经
        在线程中运行的第三方函数。因此应用层仍需要限制调用频率，并避免无意义并发。
    """

    def __init__(
        self,
        *,
        loader: FundNavLoader = _load_unit_nav,
        cache: QuoteCache | None = None,
    ) -> None:
        self._loader = loader
        self._cache = cache if cache is not None else QuoteCache(_DEFAULT_CACHE_TTL_SECONDS)
        self._closed = False

    async def get_quote(self, symbol: str) -> Quote:
        """查询基金最新单位净值，并转换为统一行情。

        Args:
            symbol: 六位中国开放式基金代码，例如 ``017811``。

        Returns:
            价格为单位净值、币种为 CNY 且标记延迟的统一 ``Quote``。

        Raises:
            MarketDataClosedError: Provider 已关闭。
            ValueError: 基金代码不是六位数字。
            MarketDataConnectionError: AKShare 请求过程中发生系统或网络 I/O 错误。
            MarketDataNotFoundError: 返回空数据。
            MarketDataResponseError: 返回字段、日期或净值无效。
        """

        if self._closed:
            raise MarketDataClosedError("AkShareFundNavProvider 已关闭")

        normalized_symbol = normalize_symbol(symbol)
        if _FUND_CODE_PATTERN.fullmatch(normalized_symbol) is None:
            raise ValueError("AKShare 开放式基金代码必须是六位数字")

        cached_quote = self._cache.get(normalized_symbol)
        if cached_quote is not None:
            return cached_quote

        try:
            # AKShare 使用同步 requests；放入工作线程后，模型流式输出和其他异步任务不会
            # 因这次网络等待而冻结。
            frame = await asyncio.to_thread(self._loader, normalized_symbol)
        except (OSError, RequestException) as error:
            # AKShare 当前通过 requests 访问东方财富。requests 的连接、DNS、超时异常
            # 继承 RequestException，并不保证继承内置 OSError，所以需要同时捕获两类异常。
            raise MarketDataConnectionError(f"AKShare 查询基金 {normalized_symbol} 失败") from error
        except Exception as error:
            # 未知第三方异常统一包裹，但不把原始网页内容或冗长堆栈放进用户消息。
            raise MarketDataResponseError(
                f"AKShare 无法解析基金 {normalized_symbol} 数据"
            ) from error

        quote = _build_latest_quote(normalized_symbol, frame)
        self._cache.put(quote)
        return quote

    async def close(self) -> None:
        """标记Provider关闭并清空缓存；重复关闭保持幂等。"""

        self._closed = True
        self._cache.clear()
