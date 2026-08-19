"""绑定 AsyncSession 的 SQLAlchemy Repository 实现。

Repository 只负责 ORM 查询、写入和领域模型转换，不调用 ``commit``。同一个 Unit of Work
创建的两个 Repository 共用一条 Session，最终由应用服务决定整笔业务是提交还是回滚。
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finagent.dashboard.models import ManualPriceRecord
from finagent.persistence.errors import PersistenceError
from finagent.persistence.models import HoldingRow, ManualPriceRow
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
from finagent.portfolio.models import Currency, Holding, HoldingCreate, HoldingUpdate


class SqlAlchemyHoldingRepository:
    """在当前 AsyncSession 中持久化规范持仓。"""

    def __init__(
        self,
        session: AsyncSession,
        catalog: AssetCatalog = DEFAULT_ASSET_CATALOG,
    ) -> None:
        self._session = session
        self._catalog = catalog

    async def list_holdings(self) -> tuple[Holding, ...]:
        """按资产代码排序返回全部持仓。"""

        rows = (
            await self._session.scalars(select(HoldingRow).order_by(HoldingRow.symbol))
        ).all()
        return tuple(self._row_to_domain(row) for row in rows)

    async def get_holding(self, symbol: str) -> Holding:
        """读取持仓，不存在时转换为稳定领域异常。"""

        row = await self._require_row(symbol)
        return self._row_to_domain(row)

    async def create_holding(self, data: HoldingCreate) -> Holding:
        """使用资产目录元数据创建新持仓，并拒绝重复代码。"""

        asset = self._catalog.require_holding_asset(data.symbol)
        if await self._session.get(HoldingRow, asset.symbol) is not None:
            raise DuplicateHoldingError(f"持仓已存在：{asset.symbol}")

        row = HoldingRow(
            symbol=asset.symbol,
            quantity=data.quantity,
            average_cost=data.average_cost,
            estimated_exit_fee_percent=data.estimated_exit_fee_percent,
            currency=asset.currency.value,
        )
        self._session.add(row)
        try:
            # flush 让数据库约束在 commit 前执行；若失败，Unit of Work 会统一回滚 Session。
            await self._session.flush()
        except IntegrityError as error:
            raise DuplicateHoldingError(f"持仓已存在：{asset.symbol}") from error
        return self._row_to_domain(row)

    async def update_holding(self, symbol: str, data: HoldingUpdate) -> Holding:
        """更新持仓数值字段，不允许改变主键和资产目录元数据。"""

        row = await self._require_row(symbol)
        row.quantity = data.quantity
        row.average_cost = data.average_cost
        row.estimated_exit_fee_percent = data.estimated_exit_fee_percent
        await self._session.flush()
        return self._row_to_domain(row)

    async def delete_holding(self, symbol: str) -> Holding:
        """删除并返回持仓；提交与关联资源清理由 Unit of Work 上层决定。"""

        row = await self._require_row(symbol)
        holding = self._row_to_domain(row)
        await self._session.delete(row)
        await self._session.flush()
        return holding

    async def load_demo(self, items: Sequence[HoldingCreate]) -> tuple[Holding, ...]:
        """仅在空表中一次加入整组演示持仓。"""

        existing_symbol = await self._session.scalar(select(HoldingRow.symbol).limit(1))
        if existing_symbol is not None:
            raise DemoPortfolioConflictError("仓库已有持仓，不能载入演示组合")

        rows_by_symbol: dict[str, HoldingRow] = {}
        for item in items:
            asset = self._catalog.require_holding_asset(item.symbol)
            if asset.symbol in rows_by_symbol:
                raise DuplicateHoldingError(f"演示持仓代码重复：{asset.symbol}")
            rows_by_symbol[asset.symbol] = HoldingRow(
                symbol=asset.symbol,
                quantity=item.quantity,
                average_cost=item.average_cost,
                estimated_exit_fee_percent=item.estimated_exit_fee_percent,
                currency=asset.currency.value,
            )

        ordered_rows = tuple(rows_by_symbol[symbol] for symbol in sorted(rows_by_symbol))
        self._session.add_all(ordered_rows)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise DuplicateHoldingError("演示持仓与现有数据冲突") from error
        return tuple(self._row_to_domain(row) for row in ordered_rows)

    async def _require_row(self, symbol: str) -> HoldingRow:
        """读取规范代码对应的 ORM Row，不存在时抛出领域异常。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        row = await self._session.get(HoldingRow, normalized_symbol)
        if row is None:
            raise HoldingNotFoundError(f"持仓不存在：{normalized_symbol}")
        return row

    def _row_to_domain(self, row: HoldingRow) -> Holding:
        """把存储字段与只读资产目录元数据组合成领域持仓。"""

        asset = self._catalog.require_holding_asset(row.symbol)
        if row.currency != asset.currency.value:
            raise PersistenceError(
                f"持仓 {row.symbol} 的数据库币种 {row.currency} 与资产目录不一致"
            )
        return Holding(
            symbol=asset.symbol,
            name=asset.name,
            asset_type=asset.asset_type,
            quantity=row.quantity,
            average_cost=row.average_cost,
            estimated_exit_fee_percent=row.estimated_exit_fee_percent,
            currency=asset.currency,
        )


class SqlAlchemyManualPriceRepository:
    """在当前 AsyncSession 中持久化手工价格记录。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_price(self, symbol: str) -> ManualPriceRecord | None:
        """读取手工价格，不存在时返回 None。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        row = await self._session.get(ManualPriceRow, normalized_symbol)
        return None if row is None else self._row_to_domain(row)

    async def save_price(self, record: ManualPriceRecord) -> ManualPriceRecord:
        """按资产代码新增或覆盖价格，保留服务端生成的录入时间。"""

        row = await self._session.get(ManualPriceRow, record.symbol)
        if row is None:
            row = ManualPriceRow(
                symbol=record.symbol,
                price=record.price,
                currency=record.currency.value,
                recorded_at=record.recorded_at,
            )
            self._session.add(row)
        else:
            row.price = record.price
            row.currency = record.currency.value
            row.recorded_at = record.recorded_at
        await self._session.flush()
        return self._row_to_domain(row)

    async def delete_price(self, symbol: str) -> ManualPriceRecord | None:
        """删除并返回手工价格；不存在时保持幂等并返回 None。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        row = await self._session.get(ManualPriceRow, normalized_symbol)
        if row is None:
            return None
        record = self._row_to_domain(row)
        await self._session.delete(row)
        await self._session.flush()
        return record

    @staticmethod
    def _row_to_domain(row: ManualPriceRow) -> ManualPriceRecord:
        """把 ORM Row 转换为 Dashboard Service 已使用的 Pydantic 模型。"""

        return ManualPriceRecord(
            symbol=row.symbol,
            price=row.price,
            currency=Currency(row.currency),
            recorded_at=row.recorded_at,
        )
