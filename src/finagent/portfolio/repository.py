"""持仓仓库协议与 Phase 6 的异步内存实现。

仓库只负责保存已经通过目录规范化的持仓，不查询行情、不保存手工价格，也不计算收益。
当前实现把数据保存在进程内存中，因此应用重启后会清空；异步协议为下一阶段 FastAPI 服务
和未来替换 SQLite 仓库保留一致调用边界。
"""

import asyncio
from collections.abc import Sequence
from typing import Protocol

from finagent.portfolio.catalog import (
    DEFAULT_ASSET_CATALOG,
    AssetCatalog,
    normalize_asset_symbol,
)
from finagent.portfolio.errors import (
    DemoPortfolioConflictError,
    DuplicateHoldingError,
    HoldingNotFoundError,
)
from finagent.portfolio.models import Holding, HoldingCreate, HoldingUpdate


class HoldingRepository(Protocol):
    """Dashboard Service 所依赖的最小异步持仓仓库接口。"""

    async def list_holdings(self) -> tuple[Holding, ...]:
        """返回全部持仓。"""

        ...

    async def get_holding(self, symbol: str) -> Holding:
        """按资产代码返回持仓。"""

        ...

    async def create_holding(self, data: HoldingCreate) -> Holding:
        """创建一项新持仓。"""

        ...

    async def update_holding(self, symbol: str, data: HoldingUpdate) -> Holding:
        """更新指定持仓的可编辑数值。"""

        ...

    async def delete_holding(self, symbol: str) -> Holding:
        """删除并返回指定持仓。"""

        ...

    async def load_demo(self, items: Sequence[HoldingCreate]) -> tuple[Holding, ...]:
        """仅在空仓库中原子载入演示持仓。"""

        ...


class InMemoryHoldingRepository:
    """使用字典和异步锁保存持仓的临时仓库。

    字典保证按代码唯一，异步锁保证多个 FastAPI 请求并发修改时，检查与写入是同一个原子
    操作。对外始终按代码排序返回元组，避免结果依赖请求到达顺序。
    """

    def __init__(self, catalog: AssetCatalog = DEFAULT_ASSET_CATALOG) -> None:
        self._catalog = catalog
        self._holdings: dict[str, Holding] = {}
        self._lock = asyncio.Lock()

    async def list_holdings(self) -> tuple[Holding, ...]:
        """返回代码升序排列的不可变持仓快照。"""

        async with self._lock:
            return self._sorted_holdings()

    async def get_holding(self, symbol: str) -> Holding:
        """读取一项持仓，不存在时抛出可映射为 HTTP 404 的领域异常。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        async with self._lock:
            return self._require_existing(normalized_symbol)

    async def create_holding(self, data: HoldingCreate) -> Holding:
        """使用目录元数据创建持仓，拒绝重复代码和仅供参考资产。"""

        holding = self._build_holding(data)
        async with self._lock:
            if holding.symbol in self._holdings:
                raise DuplicateHoldingError(f"持仓已存在：{holding.symbol}")
            self._holdings[holding.symbol] = holding
            return holding

    async def update_holding(self, symbol: str, data: HoldingUpdate) -> Holding:
        """更新数量、均价和费率，同时保持代码与目录元数据不变。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        async with self._lock:
            existing = self._require_existing(normalized_symbol)
            updated = Holding.model_validate(
                {
                    "symbol": existing.symbol,
                    "name": existing.name,
                    "asset_type": existing.asset_type,
                    "quantity": data.quantity,
                    "average_cost": data.average_cost,
                    "estimated_exit_fee_percent": data.estimated_exit_fee_percent,
                    "currency": existing.currency,
                }
            )
            self._holdings[normalized_symbol] = updated
            return updated

    async def delete_holding(self, symbol: str) -> Holding:
        """删除并返回持仓，让上层能继续执行相关资源清理。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        async with self._lock:
            existing = self._require_existing(normalized_symbol)
            del self._holdings[normalized_symbol]
            return existing

    async def load_demo(self, items: Sequence[HoldingCreate]) -> tuple[Holding, ...]:
        """仅在空仓库中原子载入一组演示持仓。

        本方法只保证“空仓、完整校验、一次性写入”。是否处于 Fake 模式以及具体使用哪组匿名
        数据，由下一阶段的 Dashboard Service 决定。任一条目不合法或代码重复时，仓库保持为空。

        Raises:
            DemoPortfolioConflictError: 仓库已经存在持仓。
            DuplicateHoldingError: 演示数据内部存在重复代码。
            UnsupportedAssetError: 演示数据包含目录外资产。
            AssetNotHoldableError: 演示数据尝试把参考资产创建为持仓。
        """

        async with self._lock:
            if self._holdings:
                raise DemoPortfolioConflictError("仓库已有持仓，不能载入演示组合")

            pending: dict[str, Holding] = {}
            for item in items:
                holding = self._build_holding(item)
                if holding.symbol in pending:
                    raise DuplicateHoldingError(f"演示持仓代码重复：{holding.symbol}")
                pending[holding.symbol] = holding

            # 所有输入都通过目录和重复校验后才一次性写入，防止只载入前半批数据。
            self._holdings.update(pending)
            return self._sorted_holdings()

    def _build_holding(self, data: HoldingCreate) -> Holding:
        """把用户可编辑字段与资产目录元数据组合成规范持仓。"""

        asset = self._catalog.require_holding_asset(data.symbol)
        return Holding.model_validate(
            {
                "symbol": asset.symbol,
                "name": asset.name,
                "asset_type": asset.asset_type,
                "quantity": data.quantity,
                "average_cost": data.average_cost,
                "estimated_exit_fee_percent": data.estimated_exit_fee_percent,
                "currency": asset.currency,
            }
        )

    def _require_existing(self, symbol: str) -> Holding:
        """在已经持有异步锁时读取持仓，避免重复加锁造成死锁。"""

        try:
            return self._holdings[symbol]
        except KeyError as error:
            raise HoldingNotFoundError(f"持仓不存在：{symbol}") from error

    def _sorted_holdings(self) -> tuple[Holding, ...]:
        """在已经持有异步锁时构建确定性持仓快照。"""

        return tuple(self._holdings[symbol] for symbol in sorted(self._holdings))
