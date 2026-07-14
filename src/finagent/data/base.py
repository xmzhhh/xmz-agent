"""与行情供应商无关的 Provider 协议和代码规范化规则。

投资组合服务只依赖 ``MarketDataProvider``，未来的真实 HTTP 适配器、本地缓存适配器和
测试假实现都遵守相同异步接口。这样更换数据源时，不需要修改计算公式或 Agent 循环。
"""

import re
from typing import Protocol, runtime_checkable

from finagent.portfolio import Quote

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9._-]+$")


def normalize_symbol(symbol: str) -> str:
    """去除首尾空格并统一为大写，同时拒绝空值和非法字符。"""

    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("资产代码不能为空")
    if _SYMBOL_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"资产代码包含不支持的字符：{symbol}")
    return normalized


@runtime_checkable
class MarketDataProvider(Protocol):
    """任意市场数据适配器必须提供的最小异步能力。"""

    async def get_quote(self, symbol: str) -> Quote:
        """查询单个规范化资产代码的最新可用行情。"""

        ...

    async def close(self) -> None:
        """释放 HTTP 连接、文件句柄或其他 Provider 资源。"""

        ...
