"""加仓、卖出试算、卖出落账和已实现收益的确定性业务服务。

TransactionService 不查询行情，也不调用大模型。用户或上层 API 提供已经确认的数量、单价和
手续费金额；Service 负责金额舍入、FIFO 批次分摊，并在同一个 Unit of Work 中同步当前持仓、
交易流水和买入批次。试算只读取数据，只有显式 ``record_sell`` 才会写库。
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from finagent.ledger.errors import (
    FutureTransactionError,
    InsufficientHoldingError,
    InvalidTradeFeeError,
    LedgerAlreadyInitializedError,
    LedgerManagedHoldingError,
    LedgerStateConflictError,
    NonChronologicalTransactionError,
    TradeAmountTooSmallError,
    UntrackedHoldingError,
)
from finagent.ledger.models import (
    BuyRequest,
    BuyResult,
    LedgerTransaction,
    LedgerTransactionCreate,
    LotConsumption,
    OpeningPositionRequest,
    OpeningPositionResult,
    PurchaseLot,
    PurchaseLotCreate,
    SellPreview,
    SellRequest,
    SellResult,
    TransactionType,
)
from finagent.ledger.unit_of_work import LedgerUnitOfWork, LedgerUnitOfWorkFactory
from finagent.portfolio.catalog import DEFAULT_ASSET_CATALOG, AssetCatalog
from finagent.portfolio.errors import HoldingNotFoundError
from finagent.portfolio.models import Holding, HoldingCreate, HoldingUpdate
from finagent.portfolio.rounding import (
    ZERO_MONEY,
    round_financial,
    round_money,
)

type Clock = Callable[[], datetime]


class TransactionService:
    """以数据库事务为边界维护当前持仓和不可变交易历史。

    Args:
        unit_of_work_factory: 提供持仓、流水、批次和手工价格仓库的事务工厂。
        catalog: 支持交易的只读资产目录。
        clock: 生成服务端记录时间，测试可注入固定时钟。
    """

    def __init__(
        self,
        unit_of_work_factory: LedgerUnitOfWorkFactory,
        *,
        catalog: AssetCatalog = DEFAULT_ASSET_CATALOG,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._catalog = catalog
        self._clock = clock

    async def initialize_opening_position(
        self,
        request: OpeningPositionRequest,
    ) -> OpeningPositionResult:
        """把 Phase 6 已有持仓登记为可执行 FIFO 的期初批次。

        这不是伪造原始买入交易，而是明确标记“从当前持仓快照开始记账”。用户提供的
        ``acquired_at`` 用于 FIFO 排序和将来的持有期费率提示。
        """

        now = self._current_time()
        self._validate_business_time(request.acquired_at, now)
        asset = self._catalog.require_holding_asset(request.symbol)

        async with self._unit_of_work_factory() as unit_of_work:
            holding = await unit_of_work.holdings.get_holding(asset.symbol)
            lots = await unit_of_work.purchase_lots.list_open_lots(asset.symbol)
            if lots:
                raise LedgerAlreadyInitializedError(
                    f"持仓 {asset.symbol} 已经存在可追踪买入批次"
                )
            await self._validate_chronological_time(
                unit_of_work,
                asset.symbol,
                request.acquired_at,
            )

            gross_amount = self._positive_money(
                holding.quantity * holding.average_cost
            )
            transaction_data = LedgerTransactionCreate(
                symbol=asset.symbol,
                transaction_type=TransactionType.OPENING,
                quantity=round_financial(holding.quantity),
                unit_price=round_financial(holding.average_cost),
                gross_amount=gross_amount,
                fee_amount=ZERO_MONEY,
                cash_amount=gross_amount,
                realized_pnl=None,
                currency=asset.currency,
                occurred_at=request.acquired_at,
                created_at=now,
                note=request.note or "从已有持仓快照初始化期初批次",
            )
            transaction = await unit_of_work.transactions.add(transaction_data)
            lot = await unit_of_work.purchase_lots.add(
                PurchaseLotCreate(
                    opening_transaction_id=transaction.id,
                    symbol=asset.symbol,
                    acquired_at=request.acquired_at,
                    original_quantity=round_financial(holding.quantity),
                    remaining_quantity=round_financial(holding.quantity),
                    unit_cost=round_financial(holding.average_cost),
                    created_at=now,
                    updated_at=now,
                )
            )
            await unit_of_work.commit()
            return OpeningPositionResult(
                transaction=transaction,
                purchase_lot=lot,
                holding=holding,
            )

    async def record_buy(self, request: BuyRequest) -> BuyResult:
        """记录买入、创建新批次并更新或创建当前持仓。"""

        now = self._current_time()
        self._validate_business_time(request.occurred_at, now)
        asset = self._catalog.require_holding_asset(request.symbol)
        quantity = round_financial(request.quantity)
        unit_price = round_financial(request.unit_price)
        gross_amount = self._positive_money(quantity * unit_price)
        fee_amount = round_money(request.fee_amount)
        cash_amount = round_money(gross_amount + fee_amount)
        unit_cost = round_financial(cash_amount / quantity)

        async with self._unit_of_work_factory() as unit_of_work:
            await self._validate_chronological_time(
                unit_of_work,
                asset.symbol,
                request.occurred_at,
            )
            holding = await self._find_holding(unit_of_work, asset.symbol)
            existing_lots = await unit_of_work.purchase_lots.list_open_lots(asset.symbol)

            if holding is None:
                if existing_lots:
                    raise LedgerStateConflictError(
                        f"资产 {asset.symbol} 没有当前持仓，但仍存在未卖完批次"
                    )
                updated_holding = await unit_of_work.holdings.create_holding(
                    HoldingCreate(
                        symbol=asset.symbol,
                        quantity=quantity,
                        average_cost=unit_cost,
                        estimated_exit_fee_percent=request.estimated_exit_fee_percent,
                    )
                )
            else:
                self._validate_tracked_lots(holding, existing_lots)
                remaining_cost = self._remaining_cost(existing_lots)
                new_quantity = round_financial(holding.quantity + quantity)
                new_average_cost = round_financial(
                    (remaining_cost + cash_amount) / new_quantity
                )
                updated_holding = await unit_of_work.holdings.update_holding(
                    asset.symbol,
                    HoldingUpdate(
                        quantity=new_quantity,
                        average_cost=new_average_cost,
                        estimated_exit_fee_percent=holding.estimated_exit_fee_percent,
                    ),
                )

            transaction = await unit_of_work.transactions.add(
                LedgerTransactionCreate(
                    symbol=asset.symbol,
                    transaction_type=TransactionType.BUY,
                    quantity=quantity,
                    unit_price=unit_price,
                    gross_amount=gross_amount,
                    fee_amount=fee_amount,
                    cash_amount=cash_amount,
                    realized_pnl=None,
                    currency=asset.currency,
                    occurred_at=request.occurred_at,
                    created_at=now,
                    note=request.note,
                )
            )
            lot = await unit_of_work.purchase_lots.add(
                PurchaseLotCreate(
                    opening_transaction_id=transaction.id,
                    symbol=asset.symbol,
                    acquired_at=request.occurred_at,
                    original_quantity=quantity,
                    remaining_quantity=quantity,
                    unit_cost=unit_cost,
                    created_at=now,
                    updated_at=now,
                )
            )
            await unit_of_work.commit()
            return BuyResult(
                transaction=transaction,
                purchase_lot=lot,
                holding=updated_holding,
            )

    async def preview_sell(self, request: SellRequest) -> SellPreview:
        """读取当前批次完成 FIFO 卖出试算，不提交任何数据库修改。"""

        now = self._current_time()
        self._validate_business_time(request.occurred_at, now)
        self._catalog.require_holding_asset(request.symbol)
        async with self._unit_of_work_factory() as unit_of_work:
            await self._validate_chronological_time(
                unit_of_work,
                request.symbol,
                request.occurred_at,
            )
            holding = await unit_of_work.holdings.get_holding(request.symbol)
            lots = await unit_of_work.purchase_lots.list_open_lots(request.symbol)
            self._validate_tracked_lots(holding, lots)
            return self._build_sell_preview(request, holding, lots)

    async def record_sell(self, request: SellRequest) -> SellResult:
        """按与试算相同的 FIFO 规则落账，并原子更新批次与持仓。"""

        now = self._current_time()
        self._validate_business_time(request.occurred_at, now)
        asset = self._catalog.require_holding_asset(request.symbol)

        async with self._unit_of_work_factory() as unit_of_work:
            await self._validate_chronological_time(
                unit_of_work,
                asset.symbol,
                request.occurred_at,
            )
            holding = await unit_of_work.holdings.get_holding(asset.symbol)
            lots = await unit_of_work.purchase_lots.list_open_lots(asset.symbol)
            self._validate_tracked_lots(holding, lots)
            preview = self._build_sell_preview(request, holding, lots)

            consumption_by_id = {
                consumption.lot_id: consumption for consumption in preview.lot_consumptions
            }
            for lot in lots:
                consumption = consumption_by_id.get(lot.id)
                if consumption is None:
                    continue
                await unit_of_work.purchase_lots.update_remaining(
                    lot.id,
                    round_financial(lot.remaining_quantity - consumption.quantity),
                )

            if preview.remaining_quantity == 0:
                await unit_of_work.holdings.delete_holding(asset.symbol)
                # 手工报价允许不存在；清仓时幂等清理，避免下次建仓误用旧价。
                await unit_of_work.manual_prices.delete_price(asset.symbol)
                updated_holding = None
            else:
                if preview.remaining_average_cost is None:
                    raise LedgerStateConflictError("部分卖出后缺少剩余平均成本")
                updated_holding = await unit_of_work.holdings.update_holding(
                    asset.symbol,
                    HoldingUpdate(
                        quantity=preview.remaining_quantity,
                        average_cost=preview.remaining_average_cost,
                        estimated_exit_fee_percent=holding.estimated_exit_fee_percent,
                    ),
                )

            transaction = await unit_of_work.transactions.add(
                LedgerTransactionCreate(
                    symbol=asset.symbol,
                    transaction_type=TransactionType.SELL,
                    quantity=preview.quantity,
                    unit_price=preview.unit_price,
                    gross_amount=preview.gross_amount,
                    fee_amount=preview.fee_amount,
                    cash_amount=preview.estimated_cash_amount,
                    realized_pnl=preview.estimated_realized_pnl,
                    currency=asset.currency,
                    occurred_at=request.occurred_at,
                    created_at=now,
                    note=request.note,
                )
            )
            await unit_of_work.commit()
            return SellResult(
                transaction=transaction,
                preview=preview,
                holding=updated_holding,
            )

    async def list_transactions(
        self,
        symbol: str | None = None,
    ) -> tuple[LedgerTransaction, ...]:
        """查询完整交易历史；不会访问行情或修改持仓。"""

        if symbol is not None:
            self._catalog.require_holding_asset(symbol)
        async with self._unit_of_work_factory() as unit_of_work:
            return await unit_of_work.transactions.list_transactions(symbol)

    async def ensure_snapshot_editable(self, symbol: str) -> None:
        """阻止已有账本历史的资产继续通过旧持仓 CRUD 绕过流水修改。"""

        asset = self._catalog.require_holding_asset(symbol)
        async with self._unit_of_work_factory() as unit_of_work:
            latest = await unit_of_work.transactions.latest_occurred_at(asset.symbol)
        if latest is not None:
            raise LedgerManagedHoldingError(
                f"资产 {asset.symbol} 已由交易账本管理，请使用加仓或卖出功能"
            )

    async def get_realized_pnl(self, symbol: str | None = None) -> Decimal:
        """汇总已确认 SELL 流水的已实现收益，不把持仓浮盈亏混入其中。"""

        transactions = await self.list_transactions(symbol)
        return round_money(
            sum(
                (
                    transaction.realized_pnl
                    for transaction in transactions
                    if transaction.transaction_type is TransactionType.SELL
                    and transaction.realized_pnl is not None
                ),
                start=Decimal("0"),
            )
        )

    def _build_sell_preview(
        self,
        request: SellRequest,
        holding: Holding,
        lots: tuple[PurchaseLot, ...],
    ) -> SellPreview:
        """纯计算 FIFO 批次扣减、到账金额和已实现收益。"""

        quantity = round_financial(request.quantity)
        unit_price = round_financial(request.unit_price)
        if quantity > holding.quantity:
            raise InsufficientHoldingError(
                f"资产 {holding.symbol} 当前只有 {holding.quantity}，不能卖出 {quantity}"
            )

        gross_amount = self._positive_money(quantity * unit_price)
        fee_amount = round_money(request.fee_amount)
        if fee_amount > gross_amount:
            raise InvalidTradeFeeError(
                f"卖出手续费 {fee_amount} 不能超过卖出金额 {gross_amount}"
            )
        cash_amount = round_money(gross_amount - fee_amount)

        quantity_left = quantity
        consumptions: list[LotConsumption] = []
        remaining_quantity = Decimal("0")
        remaining_cost = Decimal("0")
        for lot in lots:
            consumed = min(quantity_left, lot.remaining_quantity)
            if consumed > 0:
                consumptions.append(
                    LotConsumption(
                        lot_id=lot.id,
                        acquired_at=lot.acquired_at,
                        quantity=round_financial(consumed),
                        unit_cost=lot.unit_cost,
                        cost_basis=round_money(consumed * lot.unit_cost),
                    )
                )
                quantity_left -= consumed

            lot_remaining = lot.remaining_quantity - consumed
            remaining_quantity += lot_remaining
            remaining_cost += lot_remaining * lot.unit_cost

        if quantity_left != 0:
            raise LedgerStateConflictError(
                f"资产 {holding.symbol} 的批次数量不足以完成卖出"
            )

        fifo_cost_basis = round_money(
            sum(
                (consumption.cost_basis for consumption in consumptions),
                start=Decimal("0"),
            )
        )
        normalized_remaining = round_financial(remaining_quantity)
        remaining_average_cost = (
            None
            if normalized_remaining == 0
            else round_financial(remaining_cost / normalized_remaining)
        )
        return SellPreview(
            symbol=holding.symbol,
            quantity=quantity,
            unit_price=unit_price,
            gross_amount=gross_amount,
            fee_amount=fee_amount,
            estimated_cash_amount=cash_amount,
            fifo_cost_basis=fifo_cost_basis,
            estimated_realized_pnl=round_money(cash_amount - fifo_cost_basis),
            remaining_quantity=normalized_remaining,
            remaining_average_cost=remaining_average_cost,
            lot_consumptions=tuple(consumptions),
        )

    @staticmethod
    async def _find_holding(
        unit_of_work: LedgerUnitOfWork,
        symbol: str,
    ) -> Holding | None:
        """把仓库的 not-found 异常转换为买入流程需要的可选持仓。"""

        try:
            return await unit_of_work.holdings.get_holding(symbol)
        except HoldingNotFoundError:
            return None

    @staticmethod
    def _validate_tracked_lots(
        holding: Holding,
        lots: tuple[PurchaseLot, ...],
    ) -> None:
        """确认当前持仓已经初始化，且批次总量与持仓数量完全一致。"""

        if not lots:
            raise UntrackedHoldingError(
                f"持仓 {holding.symbol} 尚无买入批次，请先初始化期初持仓"
            )
        lot_quantity = round_financial(
            sum((lot.remaining_quantity for lot in lots), start=Decimal("0"))
        )
        holding_quantity = round_financial(holding.quantity)
        if lot_quantity != holding_quantity:
            raise LedgerStateConflictError(
                f"持仓 {holding.symbol} 数量为 {holding_quantity}，"
                f"但未卖完批次合计为 {lot_quantity}"
            )

    @staticmethod
    def _remaining_cost(lots: tuple[PurchaseLot, ...]) -> Decimal:
        """计算全部未卖完批次的剩余成本，用于买入后的加权平均。"""

        return sum(
            (lot.remaining_quantity * lot.unit_cost for lot in lots),
            start=Decimal("0"),
        )

    @staticmethod
    async def _validate_chronological_time(
        unit_of_work: LedgerUnitOfWork,
        symbol: str,
        occurred_at: datetime,
    ) -> None:
        """当前版本只允许按时间追加，避免插入旧交易后必须重放全部批次。"""

        latest = await unit_of_work.transactions.latest_occurred_at(symbol)
        if latest is not None and occurred_at < latest:
            raise NonChronologicalTransactionError(
                f"交易时间 {occurred_at.isoformat()} 早于最后一笔交易 {latest.isoformat()}"
            )

    @staticmethod
    def _validate_business_time(value: datetime, now: datetime) -> None:
        """阻止把未来时间写成已经发生的交易事实。"""

        if value > now:
            raise FutureTransactionError(
                f"交易时间 {value.isoformat()} 晚于服务端当前时间 {now.isoformat()}"
            )

    @staticmethod
    def _positive_money(value: Decimal) -> Decimal:
        """金额按分舍入后仍须大于零，避免数据库约束才暴露模糊错误。"""

        rounded = round_money(value)
        if rounded <= ZERO_MONEY:
            raise TradeAmountTooSmallError("交易金额按人民币分舍入后必须大于零")
        return rounded

    def _current_time(self) -> datetime:
        """取得并校验服务端时钟，避免测试或系统配置返回无时区时间。"""

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("TransactionService 时钟必须返回带时区时间")
        return now
