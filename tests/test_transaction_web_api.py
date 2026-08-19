"""交易账本 FastAPI 端点与正式 SQLite 组合根的集成测试。

测试通过 ASGITransport 调用真实 HTTP 路由，数据库位于 pytest 临时目录，行情固定为 Fake。
目标是验证 Decimal 字符串、状态码、卖出确认边界和 Dashboard/TransactionService 共享状态。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from finagent.core.config import Settings
from finagent.web import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def transaction_api_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    """迁移临时数据库并返回不访问网络的正式应用配置。"""

    database_path = tmp_path / "transaction-api.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return Settings(
        database_path=database_path,
        market_data_mode="fake",
        _env_file=None,  # type: ignore[call-arg]
    )


@asynccontextmanager
async def open_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """显式运行 FastAPI lifespan，并在内存中发送 HTTP 请求。"""

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client


async def test_opening_position_creates_history_and_locks_snapshot_crud(
    transaction_api_settings: Settings,
) -> None:
    """旧持仓初始化后应产生 opening 流水，并禁止继续绕过账本修改快照。"""

    app = create_app(transaction_api_settings)
    async with open_client(app) as client:
        holding = await client.post(
            "/api/v1/holdings",
            json={"symbol": "017811", "quantity": "13.16", "average_cost": "3.7994"},
        )
        opening = await client.post(
            "/api/v1/transactions/opening",
            json={
                "symbol": "017811",
                "acquired_at": "2026-07-01T15:00:00+08:00",
                "note": "从蚂蚁财富持仓快照开始记账",
            },
        )
        blocked_update = await client.put(
            "/api/v1/holdings/017811",
            json={
                "quantity": "20",
                "average_cost": "3.8",
                "estimated_exit_fee_percent": "0",
            },
        )
        history = await client.get("/api/v1/transactions", params={"symbol": "017811"})
        realized = await client.get(
            "/api/v1/transactions/realized-pnl",
            params={"symbol": "017811"},
        )

    assert holding.status_code == 201
    assert opening.status_code == 201
    assert opening.json()["transaction"]["transaction_type"] == "opening"
    assert opening.json()["purchase_lot"]["remaining_quantity"] == "13.16000000"
    assert blocked_update.status_code == 409
    assert blocked_update.json()["error"]["code"] == "LedgerManagedHoldingError"
    assert [item["transaction_type"] for item in history.json()] == ["opening"]
    assert realized.json() == {
        "symbol": "017811",
        "realized_pnl": "0.00",
        "currency": "CNY",
    }


async def test_buy_preview_and_confirm_sell_form_complete_http_workflow(
    transaction_api_settings: Settings,
) -> None:
    """首次买入、只读试算和确认卖出应形成完整且可核对的黄金交易闭环。"""

    app = create_app(transaction_api_settings)
    buy_payload = {
        "symbol": "JD-ZS-GOLD",
        "quantity": "1",
        "unit_price": "878.41",
        "fee_amount": "0",
        "occurred_at": "2026-08-01T10:00:00+08:00",
        "estimated_exit_fee_percent": "0.4",
        "note": "京东积存金买入",
    }
    sell_payload = {
        "symbol": "JD-ZS-GOLD",
        "quantity": "1",
        "unit_price": "878.36",
        "fee_amount": "3.51",
        "occurred_at": "2026-08-18T10:00:00+08:00",
        "note": "京东积存金卖出",
    }

    async with open_client(app) as client:
        bought = await client.post("/api/v1/transactions/buy", json=buy_payload)
        preview = await client.post("/api/v1/transactions/sell-preview", json=sell_payload)
        before_confirm = await client.get("/api/v1/holdings/JD-ZS-GOLD")
        sold = await client.post("/api/v1/transactions/sell", json=sell_payload)
        after_confirm = await client.get("/api/v1/holdings/JD-ZS-GOLD")
        blocked_recreate = await client.post(
            "/api/v1/holdings",
            json={
                "symbol": "jd-zs-gold",
                "quantity": "1",
                "average_cost": "878.41",
            },
        )
        history = await client.get("/api/v1/transactions", params={"symbol": "JD-ZS-GOLD"})
        realized = await client.get(
            "/api/v1/transactions/realized-pnl",
            params={"symbol": "JD-ZS-GOLD"},
        )

    assert bought.status_code == 201
    assert bought.json()["holding"]["average_cost"] == "878.41000000"
    assert preview.status_code == 200
    assert preview.json()["estimated_cash_amount"] == "874.85"
    assert preview.json()["estimated_realized_pnl"] == "-3.56"
    assert before_confirm.status_code == 200
    assert sold.status_code == 201
    assert sold.json()["holding"] is None
    assert after_confirm.status_code == 404
    assert blocked_recreate.status_code == 409
    assert blocked_recreate.json()["error"]["code"] == "LedgerManagedHoldingError"
    assert [item["transaction_type"] for item in history.json()] == ["buy", "sell"]
    assert realized.json()["realized_pnl"] == "-3.56"


async def test_transaction_errors_use_stable_409_and_422_responses(
    transaction_api_settings: Settings,
) -> None:
    """未初始化批次属于状态冲突，非法手续费属于参数错误。"""

    app = create_app(transaction_api_settings)
    async with open_client(app) as client:
        await client.post(
            "/api/v1/holdings",
            json={"symbol": "017811", "quantity": "10", "average_cost": "3"},
        )
        untracked = await client.post(
            "/api/v1/transactions/sell-preview",
            json={
                "symbol": "017811",
                "quantity": "1",
                "unit_price": "4",
                "occurred_at": "2026-08-18T15:00:00+08:00",
            },
        )
        await client.post(
            "/api/v1/transactions/opening",
            json={"symbol": "017811", "acquired_at": "2026-07-01T15:00:00+08:00"},
        )
        invalid_fee = await client.post(
            "/api/v1/transactions/sell-preview",
            json={
                "symbol": "017811",
                "quantity": "1",
                "unit_price": "4",
                "fee_amount": "5",
                "occurred_at": "2026-08-18T15:00:00+08:00",
            },
        )

    assert untracked.status_code == 409
    assert untracked.json()["error"]["code"] == "UntrackedHoldingError"
    assert invalid_fee.status_code == 422
    assert invalid_fee.json()["error"]["code"] == "InvalidTradeFeeError"
