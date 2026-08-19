"""TransactionService 与 SQLite Unit of Work 的业务集成测试。

测试使用 Alembic 创建的临时数据库，不接触用户的 ``data/private/finagent.db``。重点验证
FIFO 成本、买卖金额、试算无副作用，以及持仓、流水、批次和手工价格的原子更新。
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from finagent.dashboard import ManualPriceRecord
from finagent.ledger import (
    BuyRequest,
    InsufficientHoldingError,
    LedgerAlreadyInitializedError,
    NonChronologicalTransactionError,
    OpeningPositionRequest,
    SellRequest,
    TransactionService,
    TransactionType,
    UntrackedHoldingError,
)
from finagent.persistence import DatabaseManager
from finagent.persistence.unit_of_work import SqlAlchemyDashboardUnitOfWorkFactory
from finagent.portfolio import HoldingCreate, HoldingNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def ledger_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """创建只供当前测试使用、已经迁移到 head 的 SQLite 数据库。"""

    database_path = tmp_path / "ledger.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return database_path


def _build_service(
    database_path: Path,
) -> tuple[
    SqlAlchemyDashboardUnitOfWorkFactory,
    TransactionService,
]:
    """为单个测试装配共享同一数据库 Manager 的事务服务。"""

    factory = SqlAlchemyDashboardUnitOfWorkFactory(DatabaseManager(database_path))
    return factory, TransactionService(factory, clock=lambda: NOW)


def _buy_request(**data: object) -> BuyRequest:
    """模拟 JSON/API 输入创建买入模型，同时保持测试通过严格类型检查。"""

    return BuyRequest.model_validate(data)


def _sell_request(**data: object) -> SellRequest:
    """模拟 JSON/API 输入创建卖出模型。"""

    return SellRequest.model_validate(data)


def _holding_create(**data: object) -> HoldingCreate:
    """模拟旧版持仓接口输入，便于构造尚无批次的持仓。"""

    return HoldingCreate.model_validate(data)


def _manual_price(**data: object) -> ManualPriceRecord:
    """模拟手工价格 API 输入。"""

    return ManualPriceRecord.model_validate(data)


async def test_first_buy_creates_holding_transaction_and_lot(
    ledger_database_path: Path,
) -> None:
    """首次买入应在一个事务中创建三种状态，并把买入费计入单位成本。"""

    factory, service = _build_service(ledger_database_path)
    await factory.initialize()
    try:
        result = await service.record_buy(
            _buy_request(
                symbol="017811",
                quantity="10",
                unit_price="3.50",
                fee_amount="0.01",
                occurred_at=datetime(2026, 8, 18, 15, 0, tzinfo=UTC),
            )
        )

        assert result.transaction.transaction_type is TransactionType.BUY
        assert result.transaction.gross_amount == Decimal("35.00")
        assert result.transaction.cash_amount == Decimal("35.01")
        assert result.purchase_lot.unit_cost == Decimal("3.50100000")
        assert result.holding.quantity == Decimal("10")
        assert result.holding.average_cost == Decimal("3.50100000")

        async with factory() as unit_of_work:
            assert len(await unit_of_work.transactions.list_transactions("017811")) == 1
            assert len(await unit_of_work.purchase_lots.list_open_lots("017811")) == 1
    finally:
        await factory.close()


async def test_existing_snapshot_requires_opening_lot_before_buy_or_sell(
    ledger_database_path: Path,
) -> None:
    """Phase 6 遗留持仓没有批次时必须先初始化，不能假装知道历史成本。"""

    factory, service = _build_service(ledger_database_path)
    await factory.initialize()
    try:
        async with factory() as unit_of_work:
            await unit_of_work.holdings.create_holding(
                _holding_create(symbol="017811", quantity="10", average_cost="3")
            )
            await unit_of_work.commit()

        with pytest.raises(UntrackedHoldingError, match="初始化期初持仓"):
            await service.preview_sell(
                _sell_request(
                    symbol="017811",
                    quantity="1",
                    unit_price="4",
                    occurred_at=datetime(2026, 8, 18, 15, 0, tzinfo=UTC),
                )
            )

        opening = await service.initialize_opening_position(
            OpeningPositionRequest(
                symbol="017811",
                acquired_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
            )
        )
        assert opening.transaction.transaction_type is TransactionType.OPENING
        assert opening.purchase_lot.remaining_quantity == Decimal("10")

        with pytest.raises(LedgerAlreadyInitializedError):
            await service.initialize_opening_position(
                OpeningPositionRequest(
                    symbol="017811",
                    acquired_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
                )
            )
    finally:
        await factory.close()


async def test_buy_after_opening_uses_lot_cost_for_weighted_average(
    ledger_database_path: Path,
) -> None:
    """加仓后的平均成本应由旧批次剩余成本和本次实际支出共同计算。"""

    factory, service = _build_service(ledger_database_path)
    await factory.initialize()
    try:
        async with factory() as unit_of_work:
            await unit_of_work.holdings.create_holding(
                _holding_create(symbol="017811", quantity="10", average_cost="3")
            )
            await unit_of_work.commit()
        await service.initialize_opening_position(
            OpeningPositionRequest(
                symbol="017811",
                acquired_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
            )
        )

        result = await service.record_buy(
            _buy_request(
                symbol="017811",
                quantity="5",
                unit_price="4",
                occurred_at=datetime(2026, 8, 18, 15, 0, tzinfo=UTC),
            )
        )

        assert result.holding.quantity == Decimal("15.00000000")
        assert result.holding.average_cost == Decimal("3.33333333")
        async with factory() as unit_of_work:
            lots = await unit_of_work.purchase_lots.list_open_lots("017811")
        assert [lot.unit_cost for lot in lots] == [Decimal("3"), Decimal("4")]
    finally:
        await factory.close()


async def test_sell_preview_uses_fifo_without_modifying_database(
    ledger_database_path: Path,
) -> None:
    """卖出试算应先消耗最早批次，并且不能提前修改持仓、批次或流水。"""

    factory, service = _build_service(ledger_database_path)
    await factory.initialize()
    try:
        await service.record_buy(
            _buy_request(
                symbol="017811",
                quantity="10",
                unit_price="2",
                occurred_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
            )
        )
        await service.record_buy(
            _buy_request(
                symbol="017811",
                quantity="5",
                unit_price="4",
                occurred_at=datetime(2026, 8, 1, 15, 0, tzinfo=UTC),
            )
        )

        preview = await service.preview_sell(
            _sell_request(
                symbol="017811",
                quantity="12",
                unit_price="5",
                fee_amount="1",
                occurred_at=datetime(2026, 8, 18, 15, 0, tzinfo=UTC),
            )
        )

        assert preview.gross_amount == Decimal("60.00")
        assert preview.estimated_cash_amount == Decimal("59.00")
        assert preview.fifo_cost_basis == Decimal("28.00")
        assert preview.estimated_realized_pnl == Decimal("31.00")
        assert preview.remaining_quantity == Decimal("3.00000000")
        assert preview.remaining_average_cost == Decimal("4.00000000")
        assert [item.quantity for item in preview.lot_consumptions] == [
            Decimal("10.00000000"),
            Decimal("2.00000000"),
        ]

        async with factory() as unit_of_work:
            holding = await unit_of_work.holdings.get_holding("017811")
            lots = await unit_of_work.purchase_lots.list_open_lots("017811")
            transactions = await unit_of_work.transactions.list_transactions("017811")
        assert holding.quantity == Decimal("15.00000000")
        assert [lot.remaining_quantity for lot in lots] == [Decimal("10"), Decimal("5")]
        assert len(transactions) == 2
    finally:
        await factory.close()


async def test_partial_sell_updates_lots_holding_and_realized_pnl(
    ledger_database_path: Path,
) -> None:
    """确认卖出后，FIFO 批次、当前持仓和 SELL 流水必须一起更新。"""

    factory, service = _build_service(ledger_database_path)
    await factory.initialize()
    try:
        await service.record_buy(
            _buy_request(
                symbol="017811",
                quantity="10",
                unit_price="2",
                occurred_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
            )
        )
        await service.record_buy(
            _buy_request(
                symbol="017811",
                quantity="5",
                unit_price="4",
                occurred_at=datetime(2026, 8, 1, 15, 0, tzinfo=UTC),
            )
        )

        result = await service.record_sell(
            _sell_request(
                symbol="017811",
                quantity="12",
                unit_price="5",
                fee_amount="1",
                occurred_at=datetime(2026, 8, 18, 15, 0, tzinfo=UTC),
            )
        )

        assert result.transaction.realized_pnl == Decimal("31.00")
        assert await service.get_realized_pnl("017811") == Decimal("31.00")
        assert result.holding is not None
        assert result.holding.quantity == Decimal("3.00000000")
        assert result.holding.average_cost == Decimal("4.00000000")
        async with factory() as unit_of_work:
            lots = await unit_of_work.purchase_lots.list_open_lots("017811")
        assert len(lots) == 1
        assert lots[0].remaining_quantity == Decimal("3.00000000")
        assert lots[0].unit_cost == Decimal("4.00000000")
    finally:
        await factory.close()


async def test_full_gold_sell_clears_holding_and_manual_price(
    ledger_database_path: Path,
) -> None:
    """黄金清仓应计算实际到账和亏损，并同步删除不再适用的手工卖价。"""

    factory, service = _build_service(ledger_database_path)
    await factory.initialize()
    try:
        await service.record_buy(
            _buy_request(
                symbol="JD-ZS-GOLD",
                quantity="1",
                unit_price="878.41",
                occurred_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
            )
        )
        async with factory() as unit_of_work:
            await unit_of_work.manual_prices.save_price(
                _manual_price(
                    symbol="JD-ZS-GOLD",
                    price="878.36",
                    currency="CNY",
                    recorded_at=NOW,
                )
            )
            await unit_of_work.commit()

        result = await service.record_sell(
            _sell_request(
                symbol="JD-ZS-GOLD",
                quantity="1",
                unit_price="878.36",
                fee_amount="3.51",
                occurred_at=datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
            )
        )

        assert result.preview.estimated_cash_amount == Decimal("874.85")
        assert result.transaction.realized_pnl == Decimal("-3.56")
        assert result.holding is None
        async with factory() as unit_of_work:
            with pytest.raises(HoldingNotFoundError):
                await unit_of_work.holdings.get_holding("JD-ZS-GOLD")
            assert await unit_of_work.manual_prices.get_price("JD-ZS-GOLD") is None
            assert await unit_of_work.purchase_lots.list_open_lots("JD-ZS-GOLD") == ()
    finally:
        await factory.close()


async def test_failed_sell_leaves_all_tables_unchanged(
    ledger_database_path: Path,
) -> None:
    """超量卖出必须在写入前失败，不能留下 SELL 流水或被扣减的批次。"""

    factory, service = _build_service(ledger_database_path)
    await factory.initialize()
    try:
        await service.record_buy(
            _buy_request(
                symbol="017811",
                quantity="1",
                unit_price="3",
                occurred_at=datetime(2026, 8, 1, 15, 0, tzinfo=UTC),
            )
        )

        with pytest.raises(InsufficientHoldingError):
            await service.record_sell(
                _sell_request(
                    symbol="017811",
                    quantity="2",
                    unit_price="4",
                    occurred_at=datetime(2026, 8, 18, 15, 0, tzinfo=UTC),
                )
            )

        async with factory() as unit_of_work:
            holding = await unit_of_work.holdings.get_holding("017811")
            lots = await unit_of_work.purchase_lots.list_open_lots("017811")
            transactions = await unit_of_work.transactions.list_transactions("017811")
        assert holding.quantity == Decimal("1")
        assert lots[0].remaining_quantity == Decimal("1")
        assert [item.transaction_type for item in transactions] == [TransactionType.BUY]
    finally:
        await factory.close()


async def test_backdated_transaction_is_rejected(
    ledger_database_path: Path,
) -> None:
    """当前版本不支持插入更早交易，因为那需要重放后续全部 FIFO 结果。"""

    factory, service = _build_service(ledger_database_path)
    await factory.initialize()
    try:
        await service.record_buy(
            _buy_request(
                symbol="017811",
                quantity="1",
                unit_price="3",
                occurred_at=datetime(2026, 8, 10, 15, 0, tzinfo=UTC),
            )
        )

        with pytest.raises(NonChronologicalTransactionError):
            await service.record_buy(
                _buy_request(
                    symbol="017811",
                    quantity="1",
                    unit_price="4",
                    occurred_at=datetime(2026, 8, 9, 15, 0, tzinfo=UTC),
                )
            )

        history = await service.list_transactions("017811")
        assert len(history) == 1
    finally:
        await factory.close()
