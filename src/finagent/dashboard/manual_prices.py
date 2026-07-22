"""手工价格仓库协议与 Phase 6 内存实现。

仓库只保存已经包含服务端时间的价格记录，不判断价格是否过期。新鲜度属于 Dashboard
Service 的业务规则，因为允许年龄来自应用配置，而不是存储层属性。
"""

import asyncio
from typing import Protocol

from finagent.dashboard.models import ManualPriceRecord
from finagent.portfolio.catalog import normalize_asset_symbol


class ManualPriceRepository(Protocol):
    """Dashboard Service 所依赖的最小异步手工价格仓库接口。"""

    async def get_price(self, symbol: str) -> ManualPriceRecord | None:
        """返回手工价格；尚未录入时返回 None。"""

        ...

    async def save_price(self, record: ManualPriceRecord) -> ManualPriceRecord:
        """新增或替换一条手工价格。"""

        ...

    async def delete_price(self, symbol: str) -> ManualPriceRecord | None:
        """删除并返回价格；原本不存在时返回 None。"""

        ...


class InMemoryManualPriceRepository:
    """使用异步锁保护内存字典的手工价格仓库。"""

    def __init__(self) -> None:
        self._price_by_symbol: dict[str, ManualPriceRecord] = {}
        self._lock = asyncio.Lock()

    async def get_price(self, symbol: str) -> ManualPriceRecord | None:
        """读取指定代码的手工价格快照。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        async with self._lock:
            return self._price_by_symbol.get(normalized_symbol)

    async def save_price(self, record: ManualPriceRecord) -> ManualPriceRecord:
        """按资产代码新增或完整替换价格记录。"""

        async with self._lock:
            self._price_by_symbol[record.symbol] = record
            return record

    async def delete_price(self, symbol: str) -> ManualPriceRecord | None:
        """删除价格；使用 pop 默认值让重复清理保持幂等。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        async with self._lock:
            return self._price_by_symbol.pop(normalized_symbol, None)
