"""Phase 8 SQLite 只读资产工具及持久化 Agent 集成测试。

测试使用临时 SQLite、Alembic 正式迁移、Fake 行情和 Fake ModelProvider，不读取个人数据库，
也不访问 AKShare、GoldAPI 或百炼。重点验证金融字符串、最小数据披露、工具白名单以及查询前后
持仓和流水完全一致。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from finagent.agents import PersistentToolCallingAgent
from finagent.dashboard import PortfolioDashboardService
from finagent.data import FakeMarketDataProvider, MarketDataService
from finagent.ledger import BuyRequest, SellRequest, TransactionService
from finagent.llm import (
    MessageRole,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from finagent.memory import (
    ContextAssembler,
    ConversationMessage,
    ConversationService,
    MemoryService,
)
from finagent.persistence import DatabaseManager
from finagent.persistence.memory_unit_of_work import SqlAlchemyMemoryUnitOfWorkFactory
from finagent.persistence.unit_of_work import SqlAlchemyDashboardUnitOfWorkFactory
from finagent.portfolio import Currency, PortfolioCalculator, Quote
from finagent.tools import (
    ToolExecutionError,
    ToolRegistry,
    ToolValidationError,
    create_read_only_asset_tool_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)


class NoopSummarizer:
    """短对话集成测试中不应触发的离线摘要器。"""

    async def summarize(
        self,
        previous_summary: str | None,
        messages: tuple[ConversationMessage, ...],
    ) -> str:
        """意外触发摘要时直接失败，避免测试悄悄引入额外模型行为。"""

        raise AssertionError("短对话不应触发滚动摘要")


class SequenceProvider:
    """按顺序提供工具请求和最终回答的 Fake ModelProvider。"""

    def __init__(self, outcomes: list[ModelResponse]) -> None:
        self._outcomes = iter(outcomes)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """记录 Agent 发来的完整上下文并返回下一项预设响应。"""

        self.requests.append(request)
        return next(self._outcomes)

    async def close(self) -> None:
        """Fake Provider 没有网络连接需要释放。"""


@pytest.fixture
def migrated_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """创建迁移到最新 revision 的隔离 SQLite 数据库。"""

    database_path = tmp_path / "asset-read-tools.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return database_path


def _fund_quote() -> Quote:
    """返回带来源和时间的固定基金净值，不产生外部请求。"""

    return Quote.model_validate(
        {
            "symbol": "017811",
            "price": "4.50",
            "currency": "CNY",
            "as_of": NOW,
            "source": "只读工具 Fake 基金净值",
            "is_delayed": True,
        }
    )


def _build_asset_services(
    database_manager: DatabaseManager,
) -> tuple[
    SqlAlchemyDashboardUnitOfWorkFactory,
    PortfolioDashboardService,
    TransactionService,
    FakeMarketDataProvider,
]:
    """让 Dashboard 与交易服务共享同一个 SQLite Manager 和事务工厂。"""

    factory = SqlAlchemyDashboardUnitOfWorkFactory(database_manager)
    provider = FakeMarketDataProvider((_fund_quote(),))
    dashboard = PortfolioDashboardService(
        factory,
        MarketDataService(provider),
        PortfolioCalculator(Currency.CNY),
        clock=lambda: NOW,
    )
    transactions = TransactionService(factory, clock=lambda: NOW)
    return factory, dashboard, transactions, provider


async def _seed_buy_and_sell(transactions: TransactionService) -> None:
    """写入一买一卖匿名流水，供后续只读查询验证。"""

    await transactions.record_buy(
        BuyRequest.model_validate(
            {
                "symbol": "017811",
                "quantity": "10",
                "unit_price": "3.00",
                "fee_amount": "0.10",
                "occurred_at": NOW - timedelta(days=2),
                "note": "不应发送给模型的测试备注",
            }
        )
    )
    await transactions.record_sell(
        SellRequest.model_validate(
            {
                "symbol": "017811",
                "quantity": "2",
                "unit_price": "4.00",
                "fee_amount": "0.10",
                "occurred_at": NOW - timedelta(days=1),
                "note": "同样不应出现在工具结果中",
            }
        )
    )


def _tool_call(name: str, arguments: dict[str, object]) -> ToolCall:
    """构造模型生成的标准工具调用。"""

    return ToolCall(id=f"call-{name}", name=name, arguments=arguments)


async def test_registry_exposes_only_three_read_only_asset_tools(
    migrated_database_path: Path,
) -> None:
    """独立注册中心不能混入模拟行情或任何买卖、修改、删除工具。"""

    manager = DatabaseManager(migrated_database_path)
    factory, dashboard, transactions, _ = _build_asset_services(manager)
    await factory.initialize()
    try:
        registry = create_read_only_asset_tool_registry(dashboard, transactions)
        names = tuple(definition.name for definition in registry.definitions)

        assert names == (
            "get_portfolio_snapshot",
            "get_holding_record",
            "get_transaction_ledger_summary",
        )
        assert all(
            forbidden not in name
            for name in names
            for forbidden in ("create", "update", "delete", "buy", "sell")
        )
    finally:
        await dashboard.close()


async def test_asset_tools_return_precise_data_without_modifying_sqlite(
    migrated_database_path: Path,
) -> None:
    """依次查询组合、持仓和账本后，数据库状态必须与查询前完全相同。"""

    manager = DatabaseManager(migrated_database_path)
    factory, dashboard, transactions, provider = _build_asset_services(manager)
    await factory.initialize()
    await _seed_buy_and_sell(transactions)
    registry = create_read_only_asset_tool_registry(dashboard, transactions)
    try:
        holdings_before = await dashboard.list_holdings()
        transactions_before = await transactions.list_transactions()

        portfolio_result = await registry.execute(
            _tool_call("get_portfolio_snapshot", {})
        )
        holding_result = await registry.execute(
            _tool_call("get_holding_record", {"symbol": "017811"})
        )
        ledger_result = await registry.execute(
            _tool_call(
                "get_transaction_ledger_summary",
                {"symbol": "017811", "recent_limit": 1},
            )
        )

        snapshot = portfolio_result.data["snapshot"]
        assert snapshot["portfolio"]["total_market_value"] == "36.00"
        assert snapshot["portfolio"]["positions"][0]["quantity"] == "8.00000000"
        assert snapshot["portfolio"]["positions"][0]["current_price"] == "4.50"
        assert portfolio_result.data["read_only"] is True

        assert holding_result.data["holding"]["quantity"] == "8.00000000"
        assert holding_result.data["holding"]["average_cost"] == "3.01000000"
        assert holding_result.data["read_only"] is True

        assert ledger_result.data["transaction_count"] == 2
        assert ledger_result.data["transaction_counts"] == {
            "opening": 0,
            "buy": 1,
            "sell": 1,
            "adjustment": 0,
        }
        assert ledger_result.data["buy_cash_outflow"] == "30.10"
        assert ledger_result.data["sell_cash_inflow"] == "7.90"
        assert ledger_result.data["total_fees"] == "0.20"
        assert ledger_result.data["realized_pnl"] == "1.88"
        assert len(ledger_result.data["recent_transactions"]) == 1
        assert ledger_result.data["recent_transactions"][0]["transaction_type"] == "sell"

        assert await dashboard.list_holdings() == holdings_before
        assert await transactions.list_transactions() == transactions_before
        assert provider.requested_symbols == ("017811",)
    finally:
        await dashboard.close()


async def test_ledger_tool_minimizes_data_sent_to_model(
    migrated_database_path: Path,
) -> None:
    """最近流水不得包含内部 UUID、创建时间或用户自由文本备注。"""

    manager = DatabaseManager(migrated_database_path)
    factory, dashboard, transactions, _ = _build_asset_services(manager)
    await factory.initialize()
    await _seed_buy_and_sell(transactions)
    registry = create_read_only_asset_tool_registry(dashboard, transactions)
    try:
        result = await registry.execute(
            _tool_call("get_transaction_ledger_summary", {"recent_limit": 20})
        )
        serialized = result.to_message_content()
        public_transactions = json.loads(serialized)["data"]["recent_transactions"]

        assert len(public_transactions) == 2
        assert all("id" not in transaction for transaction in public_transactions)
        assert all("created_at" not in transaction for transaction in public_transactions)
        assert all("note" not in transaction for transaction in public_transactions)
        assert "不应发送给模型" not in serialized
    finally:
        await dashboard.close()


async def test_tool_inputs_reject_unknown_fields_and_out_of_range_limit(
    migrated_database_path: Path,
) -> None:
    """模型臆造写操作字段或请求过多流水时，应在调用 Service 前被输入模型拒绝。"""

    manager = DatabaseManager(migrated_database_path)
    factory, dashboard, transactions, provider = _build_asset_services(manager)
    await factory.initialize()
    registry = create_read_only_asset_tool_registry(dashboard, transactions)
    try:
        with pytest.raises(ToolValidationError, match="参数校验失败"):
            await registry.execute(
                _tool_call("get_portfolio_snapshot", {"delete_all": True})
            )
        with pytest.raises(ToolValidationError, match="参数校验失败"):
            await registry.execute(
                _tool_call(
                    "get_transaction_ledger_summary",
                    {"recent_limit": 21},
                )
            )

        assert await dashboard.list_holdings() == ()
        assert await transactions.list_transactions() == ()
        assert provider.requested_symbols == ()
    finally:
        await dashboard.close()


async def test_missing_holding_becomes_a_recoverable_tool_error(
    migrated_database_path: Path,
) -> None:
    """合法但不存在的代码应成为 ToolExecutionError，供 Agent 向用户解释而不是崩溃。"""

    manager = DatabaseManager(migrated_database_path)
    factory, dashboard, transactions, _ = _build_asset_services(manager)
    await factory.initialize()
    registry = create_read_only_asset_tool_registry(dashboard, transactions)
    try:
        with pytest.raises(ToolExecutionError, match="持仓不存在：017811"):
            await registry.execute(
                _tool_call("get_holding_record", {"symbol": "017811"})
            )
    finally:
        await dashboard.close()


async def test_persistent_agent_can_use_sqlite_portfolio_tool(
    migrated_database_path: Path,
) -> None:
    """持久化 Agent 应执行真实只读工具，并把工具结果随完整轮次保存到同一 SQLite。"""

    manager = DatabaseManager(migrated_database_path)
    dashboard_factory, dashboard, transactions, _ = _build_asset_services(manager)
    memory_factory = SqlAlchemyMemoryUnitOfWorkFactory(manager)
    await dashboard_factory.initialize()
    await memory_factory.initialize()
    await _seed_buy_and_sell(transactions)

    conversation_service = ConversationService(
        memory_factory,
        NoopSummarizer(),
        clock=lambda: NOW,
    )
    assembler = ContextAssembler(
        conversation_service,
        MemoryService(memory_factory, clock=lambda: NOW),
    )
    portfolio_call = _tool_call("get_portfolio_snapshot", {})
    provider = SequenceProvider(
        [
            ModelResponse(model="fake-model", tool_calls=(portfolio_call,)),
            ModelResponse(model="fake-model", content="当前基金持有 8 份。"),
        ]
    )
    registry: ToolRegistry = create_read_only_asset_tool_registry(dashboard, transactions)
    agent = PersistentToolCallingAgent(
        provider,
        registry,
        assembler,
        conversation_service,
        "你是只读资产研究助手，不能执行交易。",
    )
    try:
        session = await conversation_service.create_session("只读资产查询")
        result = await agent.ask(session.id, "我当前持有多少基金？")

        assert result.answer == "当前基金持有 8 份。"
        assert result.tool_call_names == ("get_portfolio_snapshot",)
        assert [message.role for message in result.messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
            MessageRole.ASSISTANT,
        ]
        tool_content = result.messages[2].content
        assert tool_content is not None
        assert json.loads(tool_content)["data"]["snapshot"]["portfolio"]["positions"][0][
            "quantity"
        ] == "8.00000000"
        assert await dashboard.list_holdings() == (
            (await dashboard.get_holding("017811")),
        )
    finally:
        await agent.close()
        await dashboard.close()
