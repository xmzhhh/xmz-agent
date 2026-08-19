"""ORM 模型和首份 Alembic 迁移的集成测试。

测试数据库全部位于 pytest 临时目录。迁移测试验证空数据库的升级、降级和再次升级；ORM
测试验证 Decimal、UTC 时间和外键约束在真实 SQLite 文件中的行为，防止只检查 Python
类定义却遗漏数据库实际能力。
"""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from finagent.persistence import (
    Base,
    DatabaseManager,
    HoldingRow,
    LedgerTransactionRow,
    ManualPriceRow,
    PurchaseLotRow,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUSINESS_TABLES = {
    "holding_positions",
    "manual_prices",
    "ledger_transactions",
    "purchase_lots",
}


def _alembic_config(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Config:
    """创建只连接临时 SQLite 文件的 Alembic 配置。

    ``MARKET_DATA_MODE=fake`` 隔离开发者本机的真实行情配置；即使本地 ``.env`` 使用 Real
    模式，数据库结构测试也不应该要求 GoldAPI Key 或访问任何外部接口。
    """

    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


@pytest.fixture
def migrated_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """创建已经执行到最新 revision 的临时数据库。"""

    database_path = tmp_path / "finagent.db"
    command.upgrade(_alembic_config(database_path, monkeypatch), "head")
    return database_path


def _read_table_names(database_path: Path) -> set[str]:
    """直接从 SQLite 系统表读取当前数据库中的表名。"""

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def test_metadata_registers_all_persistence_tables() -> None:
    """导入 persistence 包后，Alembic 必须能看到全部 ORM 表。"""

    assert set(Base.metadata.tables) == BUSINESS_TABLES


def test_initial_migration_can_upgrade_downgrade_and_upgrade_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首份迁移必须支持从空库安装、完全回退以及重新安装。"""

    database_path = tmp_path / "migration-cycle.db"
    config = _alembic_config(database_path, monkeypatch)

    command.upgrade(config, "head")

    assert BUSINESS_TABLES.issubset(_read_table_names(database_path))
    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_list(purchase_lots)").fetchall()
        transaction_indexes = connection.execute(
            "PRAGMA index_list(ledger_transactions)"
        ).fetchall()

    assert revision == ("20260817_01",)
    assert any(
        row[2] == "ledger_transactions"
        and row[3] == "opening_transaction_id"
        and row[6] == "RESTRICT"
        for row in foreign_keys
    )
    assert any(row[1] == "ix_ledger_transactions_symbol_occurred_at" for row in transaction_indexes)

    # check 会把 ORM metadata 与当前数据库比较；没有差异才说明迁移文件未漏掉模型字段。
    command.check(config)
    command.downgrade(config, "base")
    assert BUSINESS_TABLES.isdisjoint(_read_table_names(database_path))

    command.upgrade(config, "head")
    assert BUSINESS_TABLES.issubset(_read_table_names(database_path))


def test_migration_creates_missing_database_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次安装时即使 data/private 一类父目录不存在，迁移也必须能创建数据库。

    该测试防止 Alembic 在全新项目中直接连接 SQLite，最终只返回难以理解的
    ``unable to open database file``。
    """

    database_path = tmp_path / "nested" / "private" / "finagent.db"
    assert not database_path.parent.exists()

    command.upgrade(_alembic_config(database_path, monkeypatch), "head")

    assert database_path.is_file()
    assert BUSINESS_TABLES.issubset(_read_table_names(database_path))


async def test_orm_rows_round_trip_decimal_and_utc_time(
    migrated_database_path: Path,
) -> None:
    """ORM 写入后应保持 Decimal 精度，并把东八区时间统一恢复为 UTC。"""

    manager = DatabaseManager(migrated_database_path)
    source_time = datetime(
        2026,
        8,
        17,
        16,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )
    expected_utc_time = datetime(2026, 8, 17, 8, 30, tzinfo=UTC)
    transaction_id = uuid4()

    try:
        async with manager.session() as session:
            async with session.begin():
                session.add_all(
                    (
                        HoldingRow(
                            symbol="017811",
                            quantity=Decimal("13.16"),
                            average_cost=Decimal("3.7994"),
                            estimated_exit_fee_percent=Decimal("0.5"),
                            currency="CNY",
                        ),
                        ManualPriceRow(
                            symbol="JD-ZS-GOLD",
                            price=Decimal("878.36"),
                            currency="CNY",
                            recorded_at=source_time,
                        ),
                        LedgerTransactionRow(
                            id=transaction_id,
                            symbol="017811",
                            transaction_type="opening",
                            quantity=Decimal("13.16"),
                            unit_price=Decimal("3.7994"),
                            gross_amount=Decimal("50.000104"),
                            fee_amount=Decimal("0"),
                            cash_amount=Decimal("50.000104"),
                            realized_pnl=None,
                            currency="CNY",
                            occurred_at=source_time,
                            note="测试初始持仓",
                        ),
                        PurchaseLotRow(
                            id=uuid4(),
                            opening_transaction_id=transaction_id,
                            symbol="017811",
                            acquired_at=source_time,
                            original_quantity=Decimal("13.16"),
                            remaining_quantity=Decimal("13.16"),
                            unit_cost=Decimal("3.7994"),
                        ),
                    )
                )

        async with manager.session() as session:
            holding = (
                await session.execute(
                    select(HoldingRow).where(HoldingRow.symbol == "017811")
                )
            ).scalar_one()
            manual_price = (
                await session.execute(
                    select(ManualPriceRow).where(ManualPriceRow.symbol == "JD-ZS-GOLD")
                )
            ).scalar_one()
            transaction = await session.get(LedgerTransactionRow, transaction_id)

        assert holding.quantity == Decimal("13.16000000")
        assert holding.average_cost == Decimal("3.79940000")
        assert manual_price.recorded_at == expected_utc_time
        assert transaction is not None
        assert transaction.occurred_at == expected_utc_time
    finally:
        await manager.close()


async def test_purchase_lot_rejects_unknown_opening_transaction(
    migrated_database_path: Path,
) -> None:
    """数据库外键必须阻止没有来源交易的孤立买入批次。"""

    manager = DatabaseManager(migrated_database_path)

    try:
        async with manager.session() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    session.add(
                        PurchaseLotRow(
                            id=uuid4(),
                            opening_transaction_id=uuid4(),
                            symbol="017811",
                            acquired_at=datetime(2026, 8, 17, tzinfo=UTC),
                            original_quantity=Decimal("1"),
                            remaining_quantity=Decimal("1"),
                            unit_cost=Decimal("3.8"),
                        )
                    )
    finally:
        await manager.close()
