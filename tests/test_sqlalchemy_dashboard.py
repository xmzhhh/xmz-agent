"""SQLite Repository、Unit of Work 与正式 FastAPI 组合根的集成测试。

所有数据库均由 Alembic 在 pytest 临时目录创建。测试不会读取用户的真实资产数据库，也不会
访问外部行情；目标是证明进程重启后的持久化效果，以及跨持仓和手工价格操作的事务原子性。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from finagent.core.config import Settings
from finagent.dashboard import ManualPriceRecord
from finagent.persistence import DatabaseManager, DatabaseSchemaError
from finagent.persistence.unit_of_work import SqlAlchemyDashboardUnitOfWorkFactory
from finagent.portfolio import HoldingCreate, HoldingNotFoundError
from finagent.web import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


def _migrate_database(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """使用正式 Alembic 环境把临时数据库升级到最新版本。"""

    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")


@pytest.fixture
def migrated_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """返回已经迁移完成、但没有业务数据的临时 SQLite 文件。"""

    database_path = tmp_path / "finagent.db"
    _migrate_database(database_path, monkeypatch)
    return database_path


@asynccontextmanager
async def open_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """显式运行 FastAPI lifespan，并把请求直接交给 ASGI 应用。"""

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


async def test_unit_of_work_persists_only_after_commit(
    migrated_database_path: Path,
) -> None:
    """遗漏 commit 的写入必须回滚，显式 commit 后重新创建 Manager 仍能读取。"""

    manager = DatabaseManager(migrated_database_path)
    factory = SqlAlchemyDashboardUnitOfWorkFactory(manager)
    holding_input = HoldingCreate.model_validate(
        {
            "symbol": "017811",
            "quantity": "13.16",
            "average_cost": "3.7994",
        }
    )

    await factory.initialize()
    async with factory() as unit_of_work:
        await unit_of_work.holdings.create_holding(holding_input)
        # 故意不调用 commit，退出工作单元后这次写入必须消失。

    async with factory() as unit_of_work:
        with pytest.raises(HoldingNotFoundError):
            await unit_of_work.holdings.get_holding("017811")

    async with factory() as unit_of_work:
        await unit_of_work.holdings.create_holding(holding_input)
        await unit_of_work.commit()
    await factory.close()

    restarted_factory = SqlAlchemyDashboardUnitOfWorkFactory(
        DatabaseManager(migrated_database_path)
    )
    try:
        await restarted_factory.initialize()
        async with restarted_factory() as unit_of_work:
            holding = await unit_of_work.holdings.get_holding("017811")
        assert str(holding.quantity) == "13.16000000"
        assert str(holding.average_cost) == "3.79940000"
    finally:
        await restarted_factory.close()


async def test_unit_of_work_rolls_back_holding_and_price_together(
    migrated_database_path: Path,
) -> None:
    """跨两张表写入期间出现异常时，两项数据都不能残留。"""

    factory = SqlAlchemyDashboardUnitOfWorkFactory(
        DatabaseManager(migrated_database_path)
    )
    await factory.initialize()

    try:
        with pytest.raises(RuntimeError, match="模拟后续步骤失败"):
            async with factory() as unit_of_work:
                await unit_of_work.holdings.create_holding(
                    HoldingCreate.model_validate(
                        {
                            "symbol": "JD-ZS-GOLD",
                            "quantity": "1",
                            "average_cost": "878.41",
                        }
                    )
                )
                await unit_of_work.manual_prices.save_price(
                    ManualPriceRecord.model_validate(
                        {
                            "symbol": "JD-ZS-GOLD",
                            "price": "878.36",
                            "currency": "CNY",
                            "recorded_at": NOW,
                        }
                    )
                )
                raise RuntimeError("模拟后续步骤失败")

        async with factory() as unit_of_work:
            assert await unit_of_work.holdings.list_holdings() == ()
            assert await unit_of_work.manual_prices.get_price("JD-ZS-GOLD") is None
    finally:
        await factory.close()


async def test_default_app_keeps_holdings_and_price_after_restart(
    migrated_database_path: Path,
) -> None:
    """正式组合根重建 FastAPI 应用后，应从同一 SQLite 文件恢复状态。"""

    settings = Settings(
        database_path=migrated_database_path,
        market_data_mode="fake",
        _env_file=None,  # type: ignore[call-arg]
    )
    create_payload = {
        "symbol": "JD-ZS-GOLD",
        "quantity": "1",
        "average_cost": "878.41",
        "estimated_exit_fee_percent": "0.40",
    }

    first_app = create_app(settings)
    async with open_client(first_app) as client:
        assert (await client.post("/api/v1/holdings", json=create_payload)).status_code == 201
        assert (
            await client.put(
                "/api/v1/manual-prices/JD-ZS-GOLD",
                json={"price": "878.36"},
            )
        ).status_code == 200

    second_app = create_app(settings)
    async with open_client(second_app) as client:
        holding = await client.get("/api/v1/holdings/JD-ZS-GOLD")
        price = await client.get("/api/v1/manual-prices/JD-ZS-GOLD")
        duplicate = await client.post("/api/v1/holdings", json=create_payload)

    assert holding.status_code == 200
    assert holding.json()["quantity"] == "1.00000000"
    assert price.status_code == 200
    assert price.json()["price"] == "878.36000000"
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DuplicateHoldingError"


async def test_default_app_rejects_uninitialized_database(tmp_path: Path) -> None:
    """正式应用不能静默建表，应提示开发者先执行 Alembic 升级。"""

    settings = Settings(
        database_path=tmp_path / "not-migrated.db",
        market_data_mode="fake",
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app(settings)

    with pytest.raises(DatabaseSchemaError, match="alembic upgrade head"):
        async with app.router.lifespan_context(app):
            pass
