"""在多个真实行情 Provider 之间执行确定性选择的组合 Provider。

具体 Provider 已经负责连接供应商、解析响应和管理各自缓存；本模块只解决“一个资产代码
应该交给哪个 Provider”这个问题。上层因此只需依赖统一的 ``MarketDataProvider`` 协议，
不需要认识 AKShare、GoldAPI 或它们的调用细节。

当前版本故意不根据“六位数字”猜测资产类型，因为中国基金代码和股票代码可能重复。
调用方必须显式传入允许作为基金查询的代码集合；没有配置的代码会在访问任何外部数据源
之前被拒绝，避免把股票误发给基金接口。
"""

import re
from collections.abc import Collection

from finagent.data.base import MarketDataProvider, normalize_symbol
from finagent.data.errors import (
    MarketDataClosedError,
    UnsupportedMarketDataSymbolError,
)
from finagent.data.goldapi import GOLD_REFERENCE_SYMBOL
from finagent.portfolio import Quote

_FUND_SYMBOL_PATTERN = re.compile(r"^\d{6}$")


class RoutingMarketDataProvider:
    """按显式资产代码规则把查询转发给基金或黄金 Provider。

    Args:
        fund_provider: 负责查询开放式公募基金净值的 Provider。
        fund_symbols: 当前系统明确允许作为基金查询的六位代码集合。
        gold_provider: 负责查询 ``XAU-CNY-GRAM`` 国际黄金参考价的 Provider。

    Important:
        Router 接管两个子 Provider 的生命周期。调用 ``close`` 后会关闭两个子 Provider，
        因此同一子 Provider 不应再被其他 Router 或 Service 共享使用。

        Router 不捕获子 Provider 的领域异常。已经选中正确数据源后发生的超时、连接失败、
        限流或无数据，必须保留原异常类型，让上层能够区分“没有路由”和“数据源失败”。
    """

    def __init__(
        self,
        *,
        fund_provider: MarketDataProvider,
        fund_symbols: Collection[str],
        gold_provider: MarketDataProvider,
    ) -> None:
        normalized_fund_symbols: set[str] = set()
        for symbol in fund_symbols:
            normalized_symbol = normalize_symbol(symbol)
            if _FUND_SYMBOL_PATTERN.fullmatch(normalized_symbol) is None:
                raise ValueError(f"基金路由代码必须是六位数字：{symbol}")
            normalized_fund_symbols.add(normalized_symbol)

        self._fund_provider = fund_provider
        self._fund_symbols = frozenset(normalized_fund_symbols)
        self._gold_provider = gold_provider
        self._closed = False

    async def get_quote(self, symbol: str) -> Quote:
        """选择唯一子 Provider 并原样返回其统一行情。

        Args:
            symbol: 待查询资产代码。基金必须已加入构造函数的 ``fund_symbols``；黄金目前只
                支持 ``XAU-CNY-GRAM``。

        Returns:
            被选中子 Provider 返回的原始 ``Quote`` 对象。Router 不修改价格、币种、时间、
            来源或延迟标记。

        Raises:
            MarketDataClosedError: Router 已关闭。
            ValueError: 资产代码为空或包含项目不支持的字符。
            UnsupportedMarketDataSymbolError: 代码合法，但当前没有为其配置数据源。
            MarketDataError: 被选中子 Provider 原样传播的具体领域异常。
        """

        if self._closed:
            raise MarketDataClosedError("RoutingMarketDataProvider 已关闭")

        normalized_symbol = normalize_symbol(symbol)
        if normalized_symbol in self._fund_symbols:
            return await self._fund_provider.get_quote(normalized_symbol)
        if normalized_symbol == GOLD_REFERENCE_SYMBOL:
            return await self._gold_provider.get_quote(normalized_symbol)

        # 不遍历 Provider“碰运气”。没有路由是一项确定的配置错误，必须在发出外部请求前失败。
        raise UnsupportedMarketDataSymbolError(
            f"当前没有为资产代码 {normalized_symbol} 配置行情数据源"
        )

    async def close(self) -> None:
        """关闭两个子 Provider；重复调用保持幂等。"""

        if self._closed:
            return

        # 先标记关闭，避免资源释放期间又有新请求进入。当前子 Provider 的 close 均为幂等且
        # 不抛业务异常，所以使用清晰的顺序关闭，不为尚不存在的关闭失败策略增加复杂抽象。
        self._closed = True
        await self._fund_provider.close()
        await self._gold_provider.close()
