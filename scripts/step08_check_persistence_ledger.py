"""Step 08：离线验收 Phase 7 SQLite 持久化与交易账本。

本脚本在系统临时目录创建一次性 SQLite 数据库，先执行正式 Alembic 迁移，再通过 FastAPI
接口完成持仓快照、期初批次、买入、卖出试算、确认卖出和服务重启验收。行情固定使用 Fake
Provider，不读取开发者 ``.env``，不访问 AKShare、GoldAPI 或百炼，也不会修改个人数据库。
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from finagent.core.config import PROJECT_ROOT, Settings
from finagent.ledger import (
    BuyResult,
    LedgerTransaction,
    OpeningPositionResult,
    RealizedPnlSummary,
    SellPreview,
    SellResult,
)
from finagent.portfolio import Holding
from finagent.web import create_app

FUND_SYMBOL = "017811"
INITIAL_QUANTITY = "10"
INITIAL_AVERAGE_COST = "3.00"
EXPECTED_QUANTITY_AFTER_BUY = "12.00000000"
EXPECTED_AVERAGE_COST_AFTER_BUY = "3.09166667"
EXPECTED_SELL_CASH = "15.92"
EXPECTED_SELL_FIFO_COST = "12.00"
EXPECTED_REALIZED_PNL = "3.92"
EXPECTED_FINAL_QUANTITY = "8.00000000"
EXPECTED_FINAL_AVERAGE_COST = "3.13750000"


class PersistenceLedgerAcceptanceError(RuntimeError):
    """表示请求已经执行，但结果不符合 Phase 7 验收约定。"""


def _require(condition: bool, message: str) -> None:
    """在验收条件不满足时抛出包含业务语义的异常。"""

    if not condition:
        raise PersistenceLedgerAcceptanceError(message)


def _require_status(response: httpx.Response, expected: int, label: str) -> None:
    """检查 HTTP 状态码，失败时避免输出可能包含私有数据的完整正文。"""

    if response.status_code != expected:
        raise PersistenceLedgerAcceptanceError(
            f"{label} 状态码应为 {expected}，实际为 {response.status_code}"
        )


def _upgrade_database(database_path: Path) -> None:
    """让 Alembic 在临时数据库上执行与正式应用相同的迁移历史。

    Alembic 的 ``env.py`` 从环境变量读取数据库位置。这里只在迁移调用期间临时覆盖变量，
    上下文退出后自动恢复，不会改变用户的终端或 ``.env``。
    """

    environment = {
        "DATABASE_PATH": str(database_path),
        "MARKET_DATA_MODE": "fake",
    }
    with patch.dict(os.environ, environment, clear=False):
        command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")


def _settings(database_path: Path) -> Settings:
    """创建不读取 ``.env``、只连接本次临时数据库的 Fake 配置。"""

    # ``model_validate`` 直接解析给定字典，不经过 BaseSettings 的环境变量来源。
    return Settings.model_validate(
        {
            "database_path": database_path,
            "market_data_mode": "fake",
        }
    )


@asynccontextmanager
async def _open_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """启动一份新的正式 FastAPI 应用，并在退出时完整关闭数据库连接。"""

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client


async def _check_database(database_path: Path) -> None:
    """在同一数据库上依次启动三份应用，验证写入、重启和再次读取。"""

    settings = _settings(database_path)
    now = datetime.now(UTC)
    opening_at = now - timedelta(days=60)
    buy_at = now - timedelta(days=20)
    sell_at = now - timedelta(days=1)

    # 第一份应用模拟“把已有持仓迁移到账本，再进行一次加仓”。
    async with _open_client(settings) as first_client:
        page = await first_client.get("/")
        _require_status(page, 200, "资产面板页面")
        _require('id="opening-form"' in page.text, "页面缺少期初持仓表单")
        _require('id="sell-form"' in page.text, "页面缺少卖出试算表单")

        snapshot_response = await first_client.post(
            "/api/v1/holdings",
            json={
                "symbol": FUND_SYMBOL,
                "quantity": INITIAL_QUANTITY,
                "average_cost": INITIAL_AVERAGE_COST,
                "estimated_exit_fee_percent": "0.5",
            },
        )
        _require_status(snapshot_response, 201, "录入持仓快照")

        opening_response = await first_client.post(
            "/api/v1/transactions/opening",
            json={
                "symbol": FUND_SYMBOL,
                "acquired_at": opening_at.isoformat(),
                "note": "Phase 7 临时数据库期初持仓",
            },
        )
        _require_status(opening_response, 201, "初始化期初持仓")
        opening = OpeningPositionResult.model_validate(opening_response.json())
        _require(
            opening.transaction.transaction_type.value == "opening",
            "期初初始化没有生成 opening 流水",
        )

        buy_response = await first_client.post(
            "/api/v1/transactions/buy",
            json={
                "symbol": FUND_SYMBOL,
                "quantity": "2",
                "unit_price": "3.50",
                "fee_amount": "0.10",
                "occurred_at": buy_at.isoformat(),
                "estimated_exit_fee_percent": "0.5",
                "note": "Phase 7 临时数据库加仓",
            },
        )
        _require_status(buy_response, 201, "记录买入")
        buy = BuyResult.model_validate(buy_response.json())
        _require(
            str(buy.holding.quantity) == EXPECTED_QUANTITY_AFTER_BUY,
            "买入后持仓数量不符合预期",
        )
        _require(
            str(buy.holding.average_cost) == EXPECTED_AVERAGE_COST_AFTER_BUY,
            "买入手续费计入成本后的平均成本不符合预期",
        )

    print("第一进程：期初流水与买入流水已写入 SQLite")

    # 第二份应用使用同一个文件重新装配 Engine 和 Service，证明数据不是进程内假象。
    async with _open_client(settings) as second_client:
        holding_response = await second_client.get(f"/api/v1/holdings/{FUND_SYMBOL}")
        _require_status(holding_response, 200, "重启后读取持仓")
        persisted_holding = Holding.model_validate(holding_response.json())
        _require(
            str(persisted_holding.quantity) == EXPECTED_QUANTITY_AFTER_BUY,
            "服务重启后没有恢复买入后的持仓数量",
        )

        history_response = await second_client.get(
            "/api/v1/transactions",
            params={"symbol": FUND_SYMBOL},
        )
        _require_status(history_response, 200, "重启后读取交易流水")
        history = tuple(
            LedgerTransaction.model_validate(item) for item in history_response.json()
        )
        _require(
            tuple(item.transaction_type.value for item in history) == ("opening", "buy"),
            "服务重启后交易流水顺序或内容不符合预期",
        )
        print(
            "第一次重启：持仓 "
            f"{persisted_holding.quantity} 份，流水 {len(history)} 笔，数据仍然存在"
        )

        sell_payload = {
            "symbol": FUND_SYMBOL,
            "quantity": "4",
            "unit_price": "4.00",
            "fee_amount": "0.08",
            "occurred_at": sell_at.isoformat(),
            "note": "Phase 7 临时数据库卖出",
        }
        preview_response = await second_client.post(
            "/api/v1/transactions/sell-preview",
            json=sell_payload,
        )
        _require_status(preview_response, 200, "卖出试算")
        preview = SellPreview.model_validate(preview_response.json())
        _require(
            str(preview.estimated_cash_amount) == EXPECTED_SELL_CASH,
            "卖出试算的预计到账金额不符合预期",
        )
        _require(
            str(preview.fifo_cost_basis) == EXPECTED_SELL_FIFO_COST,
            "卖出试算的 FIFO 成本不符合预期",
        )
        _require(
            str(preview.estimated_realized_pnl) == EXPECTED_REALIZED_PNL,
            "卖出试算的预计已实现收益不符合预期",
        )

        before_confirm_response = await second_client.get(
            f"/api/v1/holdings/{FUND_SYMBOL}"
        )
        before_confirm = Holding.model_validate(before_confirm_response.json())
        _require(
            str(before_confirm.quantity) == EXPECTED_QUANTITY_AFTER_BUY,
            "只读卖出试算不应修改持仓数量",
        )
        print(
            "卖出试算：预计到账 "
            f"{preview.estimated_cash_amount} CNY，FIFO 成本 {preview.fifo_cost_basis} CNY，"
            f"数据库持仓仍为 {before_confirm.quantity} 份"
        )

        sell_response = await second_client.post(
            "/api/v1/transactions/sell",
            json=sell_payload,
        )
        _require_status(sell_response, 201, "确认卖出")
        sell = SellResult.model_validate(sell_response.json())
        if sell.holding is None:
            # 这里使用显式分支而不是只调用 _require，使 mypy 也能确认后续不再是 Optional。
            raise PersistenceLedgerAcceptanceError("本次部分卖出不应删除整项持仓")
        remaining_holding = sell.holding
        _require(
            str(remaining_holding.quantity) == EXPECTED_FINAL_QUANTITY,
            "确认卖出后的剩余数量不符合预期",
        )
        _require(
            str(remaining_holding.average_cost) == EXPECTED_FINAL_AVERAGE_COST,
            "FIFO 卖出后的剩余平均成本不符合预期",
        )

        realized_response = await second_client.get(
            "/api/v1/transactions/realized-pnl",
            params={"symbol": FUND_SYMBOL},
        )
        _require_status(realized_response, 200, "查询已实现收益")
        realized = RealizedPnlSummary.model_validate(realized_response.json())
        _require(
            str(realized.realized_pnl) == EXPECTED_REALIZED_PNL,
            "确认卖出后的已实现收益汇总不符合预期",
        )
        print(
            "确认卖出：剩余 "
            f"{remaining_holding.quantity} 份，累计已实现收益 {realized.realized_pnl} CNY"
        )

    # 第三份应用再次重新连接，证明确认卖出的最终状态也已经持久化。
    async with _open_client(settings) as third_client:
        final_holding_response = await third_client.get(
            f"/api/v1/holdings/{FUND_SYMBOL}"
        )
        _require_status(final_holding_response, 200, "第二次重启后读取持仓")
        final_holding = Holding.model_validate(final_holding_response.json())

        final_history_response = await third_client.get(
            "/api/v1/transactions",
            params={"symbol": FUND_SYMBOL},
        )
        _require_status(final_history_response, 200, "第二次重启后读取交易流水")
        final_history = tuple(
            LedgerTransaction.model_validate(item)
            for item in final_history_response.json()
        )
        _require(
            str(final_holding.quantity) == EXPECTED_FINAL_QUANTITY,
            "第二次重启后没有恢复卖出后的剩余持仓",
        )
        _require(
            tuple(item.transaction_type.value for item in final_history)
            == ("opening", "buy", "sell"),
            "第二次重启后没有恢复完整交易历史",
        )
        print(
            "第二次重启：剩余持仓 "
            f"{final_holding.quantity} 份，完整流水 {len(final_history)} 笔"
        )


async def check_persistence_ledger() -> bool:
    """创建一次性数据库并执行 Phase 7 端到端验收。

    Returns:
        迁移、两次重启和交易闭环全部符合约定时返回 ``True``；已经分类的迁移、HTTP、
        响应校验或数据库错误返回 ``False``。
    """

    print("=== Phase 7 SQLite 持久化与交易账本离线验收 ===")
    print("运行模式：Fake（临时 SQLite，不访问真实数据源）")

    try:
        with TemporaryDirectory(prefix="finagent-phase7-") as temporary_directory:
            database_path = Path(temporary_directory) / "phase7-acceptance.db"
            # Alembic 的异步迁移入口会创建自己的事件循环，因此放在线程中执行，避免与当前
            # 验收函数的 asyncio 事件循环冲突。
            await asyncio.to_thread(_upgrade_database, database_path)
            _require(database_path.is_file(), "Alembic 没有创建临时 SQLite 数据库")
            print("数据库迁移：已升级到当前 Alembic head")

            await _check_database(database_path)

        print("临时数据库：验收结束后已自动删除")
        print("真实网络请求：无")
        print("个人数据库修改：无")
        print("持久化与交易账本验收：通过")
        return True
    except (
        CommandError,
        PersistenceLedgerAcceptanceError,
        httpx.HTTPError,
        OSError,
        SQLAlchemyError,
        ValidationError,
    ) as error:
        print(f"错误类型：{type(error).__name__}")
        print(f"错误信息：{error}")
        print("持久化与交易账本验收：失败")
        return False


def main() -> None:
    """运行离线验收，并用非零退出码表示失败。"""

    succeeded = asyncio.run(check_persistence_ledger())
    if not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
