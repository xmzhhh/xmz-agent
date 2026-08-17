"""持仓、必要价格、组合计算和可选参考价的资产面板应用服务。

本服务是 Phase 6 的核心编排层：它从仓库读取规范持仓，为自动行情资产查询 MarketDataService，
为手工估值资产校验用户价格，再把完整数据交给纯计算 PortfolioCalculator。GoldAPI 国际金价
只作为可选参考，失败不会污染或阻断必要的组合估值。
"""

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from finagent.dashboard.errors import (
    DashboardClockError,
    DemoPortfolioUnavailableError,
    ManualPriceNotFoundError,
    ManualPriceNotSupportedError,
    ManualPriceStaleError,
)
from finagent.dashboard.manual_prices import ManualPriceRepository
from finagent.dashboard.models import (
    DashboardSnapshot,
    GoldReferenceResult,
    GoldReferenceStatus,
    ManualPriceInput,
    ManualPriceRecord,
)
from finagent.data.errors import MarketDataError
from finagent.data.goldapi import GOLD_REFERENCE_SYMBOL
from finagent.portfolio.calculator import PortfolioCalculator
from finagent.portfolio.catalog import (
    DEFAULT_ASSET_CATALOG,
    AssetCatalog,
    AssetDefinition,
    AssetValuationMethod,
    normalize_asset_symbol,
)
from finagent.portfolio.errors import DemoPortfolioConflictError
from finagent.portfolio.models import (
    Holding,
    HoldingCreate,
    HoldingUpdate,
    Quote,
)
from finagent.portfolio.repository import HoldingRepository

type Clock = Callable[[], datetime]

MANUAL_PRICE_SOURCE = "用户手工录入的京东金融卖出价"
JD_GOLD_SYMBOL = "JD-ZS-GOLD"


def _utc_now() -> datetime:
    """返回带时区的当前 UTC 时间，测试可通过构造函数注入固定时钟。"""

    return datetime.now(UTC)


class MarketDataReader(Protocol):
    """资产面板需要的最小行情服务能力。"""

    async def get_quote(self, symbol: str) -> Quote:
        """查询一条行情。"""

        ...

    async def get_quotes(self, symbols: Sequence[str]) -> tuple[Quote, ...]:
        """按顺序查询一批行情。"""

        ...

    async def close(self) -> None:
        """释放底层行情 Provider 资源。"""

        ...


ANONYMOUS_DEMO_HOLDINGS = (
    HoldingCreate.model_validate(
        {
            "symbol": "017811",
            "quantity": "100",
            "average_cost": "3.50",
            "estimated_exit_fee_percent": "0.50",
        }
    ),
    HoldingCreate.model_validate(
        {
            "symbol": JD_GOLD_SYMBOL,
            "quantity": "2",
            "average_cost": "800",
            "estimated_exit_fee_percent": "0.40",
        }
    ),
)
ANONYMOUS_DEMO_GOLD_PRICE = ManualPriceInput.model_validate({"price": "850"})


class PortfolioDashboardService:
    """对外提供资产目录、持仓管理、手工价格和面板快照。

    Args:
        holding_repository: 规范持仓的异步仓库。
        manual_price_repository: 手工价格的异步仓库。
        market_data: 已包含超时和行情校验的市场数据服务。
        calculator: 不访问外部资源的确定性组合计算器。
        catalog: 受支持资产目录。
        manual_price_max_age: 手工价格最大年龄；恰好等于阈值仍有效。
        clock: 服务端当前时间函数，必须返回带时区时间。
        demo_enabled: 只有 Fake 模式的组合根可以把它设为 True。
    """

    def __init__(
        self,
        holding_repository: HoldingRepository,
        manual_price_repository: ManualPriceRepository,
        market_data: MarketDataReader,
        calculator: PortfolioCalculator,
        *,
        catalog: AssetCatalog = DEFAULT_ASSET_CATALOG,
        manual_price_max_age: timedelta = timedelta(seconds=900),
        clock: Clock = _utc_now,
        demo_enabled: bool = False,
    ) -> None:
        if manual_price_max_age < timedelta(0):
            raise ValueError("manual_price_max_age 不能为负数")

        self._holding_repository = holding_repository
        self._manual_price_repository = manual_price_repository
        self._market_data = market_data
        self._calculator = calculator
        self._catalog = catalog
        self._manual_price_max_age = manual_price_max_age
        self._clock = clock
        self._demo_enabled = demo_enabled
        # 该锁只保护跨“持仓仓库 + 手工价格仓库”的本地状态切换，不在网络请求期间持有。
        self._state_lock = asyncio.Lock()

    def list_assets(self) -> tuple[AssetDefinition, ...]:
        """返回前端可以展示的完整受支持资产目录。"""

        return self._catalog.list_assets()

    async def list_holdings(self) -> tuple[Holding, ...]:
        """返回当前持仓快照。"""

        async with self._state_lock:
            return await self._holding_repository.list_holdings()

    async def get_holding(self, symbol: str) -> Holding:
        """按代码读取持仓。"""

        async with self._state_lock:
            return await self._holding_repository.get_holding(symbol)

    async def create_holding(self, data: HoldingCreate) -> Holding:
        """创建规范持仓；手工估值资产允许稍后再录入价格。"""

        async with self._state_lock:
            return await self._holding_repository.create_holding(data)

    async def update_holding(self, symbol: str, data: HoldingUpdate) -> Holding:
        """更新持仓的数量、成本和预计卖出费率。"""

        async with self._state_lock:
            return await self._holding_repository.update_holding(symbol, data)

    async def delete_holding(self, symbol: str) -> Holding:
        """删除持仓；手工估值资产的孤立价格会在同一临界区内同步清除。"""

        async with self._state_lock:
            deleted = await self._holding_repository.delete_holding(symbol)
            asset = self._catalog.get(deleted.symbol)
            if asset.valuation_method is AssetValuationMethod.MANUAL_PRICE:
                await self._manual_price_repository.delete_price(deleted.symbol)
            return deleted

    async def get_manual_price(self, symbol: str) -> ManualPriceRecord:
        """读取手工价格记录；本接口允许返回已经过期的记录供用户查看和更新。"""

        asset = self._require_manual_price_asset(symbol)
        async with self._state_lock:
            record = await self._manual_price_repository.get_price(asset.symbol)
        if record is None:
            raise ManualPriceNotFoundError(f"资产 {asset.symbol} 尚未录入手工卖出价")
        return record

    async def set_manual_price(
        self,
        symbol: str,
        data: ManualPriceInput,
    ) -> ManualPriceRecord:
        """使用服务端当前时间新增或替换手工价格。"""

        asset = self._require_manual_price_asset(symbol)
        record = self._build_manual_price_record(asset, data, self._current_time())
        async with self._state_lock:
            return await self._manual_price_repository.save_price(record)

    async def delete_manual_price(self, symbol: str) -> ManualPriceRecord:
        """删除手工价格，不存在时返回明确业务异常。"""

        asset = self._require_manual_price_asset(symbol)
        async with self._state_lock:
            deleted = await self._manual_price_repository.delete_price(asset.symbol)
        if deleted is None:
            raise ManualPriceNotFoundError(f"资产 {asset.symbol} 尚未录入手工卖出价")
        return deleted

    async def get_dashboard(self) -> DashboardSnapshot:
        """生成必要数据完整、可选参考价可降级的一次资产面板快照。"""

        # 只在锁内复制内存状态，随后释放锁再访问外部行情，避免慢网络阻塞持仓 CRUD。
        async with self._state_lock:
            holdings = await self._holding_repository.list_holdings()
            if not holdings:
                return DashboardSnapshot(
                    portfolio=self._calculator.calculate((), ()),
                    gold_reference=self._not_requested_reference(),
                )
            manual_records = await self._read_required_manual_prices(holdings)

        manual_quotes = tuple(
            self._manual_record_to_quote(manual_records[holding.symbol])
            for holding in holdings
            if holding.symbol in manual_records
        )
        market_symbols = tuple(
            holding.symbol
            for holding in holdings
            if self._catalog.get(holding.symbol).valuation_method
            is AssetValuationMethod.MARKET_DATA
        )
        # 手工价格已经在访问网络前完成缺失与过期校验，避免必然失败时仍消耗 API 请求。
        market_quotes = await self._market_data.get_quotes(market_symbols) if market_symbols else ()
        portfolio = self._calculator.calculate(holdings, (*manual_quotes, *market_quotes))

        has_manual_gold = any(holding.symbol == JD_GOLD_SYMBOL for holding in holdings)
        gold_reference = (
            await self._get_optional_gold_reference()
            if has_manual_gold
            else self._not_requested_reference()
        )
        return DashboardSnapshot(portfolio=portfolio, gold_reference=gold_reference)

    async def load_demo(self) -> tuple[Holding, ...]:
        """在 Fake 模式和空状态下原子载入匿名演示持仓与演示黄金卖价。"""

        if not self._demo_enabled:
            raise DemoPortfolioUnavailableError("只有 Fake 模式允许载入匿名演示组合")

        demo_record = self._build_manual_price_record(
            self._require_manual_price_asset(JD_GOLD_SYMBOL),
            ANONYMOUS_DEMO_GOLD_PRICE,
            self._current_time(),
        )
        async with self._state_lock:
            existing_price = await self._manual_price_repository.get_price(JD_GOLD_SYMBOL)
            if existing_price is not None:
                raise DemoPortfolioConflictError("已经存在手工黄金价格，不能载入演示组合")

            holdings = await self._holding_repository.load_demo(ANONYMOUS_DEMO_HOLDINGS)
            try:
                await self._manual_price_repository.save_price(demo_record)
            except Exception:
                # 当前阶段的两个仓库都在内存中。若第二次写入意外失败，回滚已载入持仓，
                # 保持“不覆盖、不合并、不留下半批数据”的演示语义。
                for holding in holdings:
                    await self._holding_repository.delete_holding(holding.symbol)
                raise
            return holdings

    async def close(self) -> None:
        """释放 Dashboard Service 所拥有的底层行情资源。"""

        await self._market_data.close()

    async def _read_required_manual_prices(
        self,
        holdings: tuple[Holding, ...],
    ) -> dict[str, ManualPriceRecord]:
        """读取并校验全部必要手工价格；任一缺失或过期都拒绝残缺快照。"""

        result: dict[str, ManualPriceRecord] = {}
        now: datetime | None = None
        for holding in holdings:
            asset = self._catalog.get(holding.symbol)
            if asset.valuation_method is not AssetValuationMethod.MANUAL_PRICE:
                continue
            if now is None:
                now = self._current_time()
            record = await self._manual_price_repository.get_price(holding.symbol)
            if record is None:
                raise ManualPriceNotFoundError(f"资产 {holding.symbol} 尚未录入手工卖出价")
            self._validate_manual_price_age(record, now)
            result[holding.symbol] = record
        return result

    async def _get_optional_gold_reference(self) -> GoldReferenceResult:
        """查询可选国际金价；只降级已知行情异常，不隐藏编程错误。"""

        try:
            quote = await self._market_data.get_quote(GOLD_REFERENCE_SYMBOL)
        except MarketDataError:
            return GoldReferenceResult(
                status=GoldReferenceStatus.UNAVAILABLE,
                message="国际黄金参考价暂不可用，不影响组合估值",
            )
        return GoldReferenceResult(status=GoldReferenceStatus.AVAILABLE, quote=quote)

    def _require_manual_price_asset(self, symbol: str) -> AssetDefinition:
        """确认代码存在且明确配置为手工价格估值。"""

        asset = self._catalog.get(normalize_asset_symbol(symbol))
        if asset.valuation_method is not AssetValuationMethod.MANUAL_PRICE:
            raise ManualPriceNotSupportedError(f"资产 {asset.symbol} 不使用手工价格估值")
        return asset

    @staticmethod
    def _build_manual_price_record(
        asset: AssetDefinition,
        data: ManualPriceInput,
        recorded_at: datetime,
    ) -> ManualPriceRecord:
        """把用户价格与服务端维护的代码、币种和时间组合成完整记录。"""

        return ManualPriceRecord(
            symbol=asset.symbol,
            price=data.price,
            currency=asset.currency,
            recorded_at=recorded_at,
        )

    def _validate_manual_price_age(
        self,
        record: ManualPriceRecord,
        now: datetime,
    ) -> None:
        """拒绝未来时间和严格超过最大年龄的手工价格。"""

        age = now - record.recorded_at
        if age < timedelta(0):
            raise ManualPriceStaleError(f"手工价格 {record.symbol} 的录入时间晚于当前时间")
        if age > self._manual_price_max_age:
            raise ManualPriceStaleError(
                f"手工价格 {record.symbol} 已过期 {age}，超过允许值 {self._manual_price_max_age}"
            )

    @staticmethod
    def _manual_record_to_quote(record: ManualPriceRecord) -> Quote:
        """把有效手工记录转换为计算器已经理解的统一 Quote。"""

        return Quote(
            symbol=record.symbol,
            price=record.price,
            currency=record.currency,
            as_of=record.recorded_at,
            source=MANUAL_PRICE_SOURCE,
            is_delayed=False,
        )

    def _current_time(self) -> datetime:
        """读取并校验服务端时钟，防止无时区时间进入年龄计算。"""

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DashboardClockError("PortfolioDashboardService 的 clock 必须返回带时区时间")
        return now

    @staticmethod
    def _not_requested_reference() -> GoldReferenceResult:
        """构造没有黄金持仓时的明确未请求状态。"""

        return GoldReferenceResult(status=GoldReferenceStatus.NOT_REQUESTED)
