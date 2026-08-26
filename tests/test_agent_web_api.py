"""正式 Agent 会话与长期记忆 FastAPI 接口的离线集成测试。

测试使用迁移后的临时 SQLite、Fake 行情和顺序 Fake 模型，完整覆盖“创建会话 → 只读工具调用
→ 回答落库 → 候选生成 → 用户确认/拒绝”的边界。自动测试不会读取本地 ``.env``，也不会
访问百炼、AKShare 或 GoldAPI。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config

from finagent.agents import AgentApplicationService, PersistentToolCallingAgent
from finagent.core.config import Settings
from finagent.dashboard import PortfolioDashboardService
from finagent.data import FakeMarketDataProvider, MarketDataService
from finagent.ledger import TransactionService
from finagent.llm import ModelRequest, ModelResponse, ToolCall
from finagent.memory import (
    ContextAssembler,
    ConversationMessage,
    ConversationService,
    MemoryService,
    ModelMemoryCandidateExtractor,
)
from finagent.persistence import DatabaseManager, SqlAlchemyMemoryUnitOfWorkFactory
from finagent.persistence.unit_of_work import SqlAlchemyDashboardUnitOfWorkFactory
from finagent.portfolio import Currency, PortfolioCalculator
from finagent.tools import create_read_only_asset_tool_registry
from finagent.web.app import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class NoopSummarizer:
    """短会话测试不应触发滚动摘要。"""

    async def summarize(
        self,
        _previous_summary: str | None,
        _messages: tuple[ConversationMessage, ...],
    ) -> str:
        """一旦被意外调用便失败，防止测试静默绕过摘要边界。"""

        raise AssertionError("短会话测试不应触发滚动摘要")


class SequenceProvider:
    """按顺序模拟主 Agent 响应和候选抽取响应。"""

    def __init__(self, outcomes: list[ModelResponse]) -> None:
        self._outcomes = iter(outcomes)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """记录请求后返回下一项固定响应。"""

        self.requests.append(request)
        return next(self._outcomes)

    async def close(self) -> None:
        """记录共享 Provider 只由 Agent 生命周期关闭。"""

        self.closed = True


@pytest.fixture
def migrated_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """创建已迁移到当前 Alembic head 的一次性数据库。"""

    database_path = tmp_path / "agent-web-api.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return database_path


@asynccontextmanager
async def open_agent_client(
    database_path: Path,
    outcomes: list[ModelResponse],
) -> AsyncIterator[tuple[httpx.AsyncClient, SequenceProvider]]:
    """用共享 SQLite 和 Fake Provider 组装接近生产结构的测试应用。"""

    settings = Settings(
        _env_file=None,
        database_path=database_path,
        market_data_mode="fake",
    )  # type: ignore[call-arg]
    manager = DatabaseManager(database_path)
    dashboard_factory = SqlAlchemyDashboardUnitOfWorkFactory(manager)
    memory_factory = SqlAlchemyMemoryUnitOfWorkFactory(manager)
    dashboard = PortfolioDashboardService(
        dashboard_factory,
        MarketDataService(FakeMarketDataProvider(())),
        PortfolioCalculator(Currency.CNY),
        manual_price_max_age=timedelta(seconds=900),
        demo_enabled=True,
    )
    transactions = TransactionService(dashboard_factory)
    conversations = ConversationService(memory_factory, NoopSummarizer())
    memories = MemoryService(memory_factory)
    provider = SequenceProvider(outcomes)
    persistent_agent = PersistentToolCallingAgent(
        provider,
        create_read_only_asset_tool_registry(dashboard, transactions),
        ContextAssembler(conversations, memories),
        conversations,
        "你是只读资产测试助手",
    )
    agent = AgentApplicationService(
        persistent_agent,
        conversations,
        memories,
        ModelMemoryCandidateExtractor(provider),
    )
    app = create_app(
        settings,
        dashboard,
        transactions,
        conversations,
        memories,
        agent,
    )
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client, provider


async def test_chat_uses_read_only_tool_then_creates_confirmable_memory(
    migrated_database_path: Path,
) -> None:
    """一轮工具对话应完整落库，候选只能由后续显式 API 确认。"""

    tool_call = ToolCall(
        id="call-dashboard",
        name="get_portfolio_snapshot",
        arguments={},
    )
    outcomes = [
        ModelResponse(model="fake-model", tool_calls=(tool_call,)),
        ModelResponse(model="fake-model", content="当前组合为空。"),
        ModelResponse(
            model="fake-model",
            content=(
                '{"candidate":{"memory_type":"preference",'
                '"memory_key":"report.data_source",'
                '"value":{"show_source":true},"scope_type":"global",'
                '"scope_id":null,"ttl_seconds":null}}'
            ),
        ),
    ]

    async with open_agent_client(migrated_database_path, outcomes) as (client, provider):
        created = await client.post(
            "/api/v1/agent/sessions",
            json={"title": "资产问答"},
        )
        session_id = created.json()["id"]
        chatted = await client.post(
            f"/api/v1/agent/sessions/{session_id}/chat",
            json={"message": "以后回答时请展示数据来源"},
        )
        messages = await client.get(
            f"/api/v1/agent/sessions/{session_id}/messages"
        )
        candidates = await client.get("/api/v1/memories/candidates")

        assert created.status_code == 201
        assert chatted.status_code == 200
        assert chatted.json()["turn"]["tool_call_names"] == [
            "get_portfolio_snapshot"
        ]
        assert chatted.json()["memory_candidate"]["status"] == "candidate"
        assert [message["role"] for message in messages.json()] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert len(candidates.json()) == 1

        memory_id = candidates.json()[0]["id"]
        confirmed = await client.post(f"/api/v1/memories/{memory_id}/confirm")
        active = await client.get("/api/v1/memories?status=active")
        events = await client.get(f"/api/v1/memories/{memory_id}/events")
        duplicate_confirm = await client.post(
            f"/api/v1/memories/{memory_id}/confirm"
        )

        assert confirmed.status_code == 200
        assert confirmed.json()["memory"]["status"] == "active"
        assert len(active.json()) == 1
        assert [event["event_type"] for event in events.json()] == [
            "candidate_created",
            "confirmed",
        ]
        assert duplicate_confirm.status_code == 409
        assert provider.requests[0].tools
        assert provider.requests[-1].tool_choice == "none"

    assert provider.closed is True


async def test_candidate_extraction_failure_preserves_saved_answer(
    migrated_database_path: Path,
) -> None:
    """候选 JSON 失败只能降级为警告，不能丢弃已经成功持久化的回答。"""

    outcomes = [
        ModelResponse(model="fake-model", content="回答已经完成。"),
        ModelResponse(model="fake-model", content="不是 JSON"),
    ]
    async with open_agent_client(migrated_database_path, outcomes) as (client, _):
        session = await client.post(
            "/api/v1/agent/sessions",
            json={"title": "降级测试"},
        )
        session_id = session.json()["id"]
        chatted = await client.post(
            f"/api/v1/agent/sessions/{session_id}/chat",
            json={"message": "这是一个普通问题"},
        )
        messages = await client.get(
            f"/api/v1/agent/sessions/{session_id}/messages"
        )

    assert chatted.status_code == 200
    assert chatted.json()["turn"]["answer"] == "回答已经完成。"
    assert "抽取失败" in chatted.json()["memory_warning"]
    assert [message["content"] for message in messages.json()] == [
        "这是一个普通问题",
        "回答已经完成。",
    ]


async def test_rejected_memory_can_be_deleted_while_audit_events_remain(
    migrated_database_path: Path,
) -> None:
    """拒绝与硬删除必须由独立 API 完成，删除后仍可查看不含正文的审计事件。"""

    outcomes = [
        ModelResponse(model="fake-model", content="已记录你的目标。"),
        ModelResponse(
            model="fake-model",
            content=(
                '{"candidate":{"memory_type":"goal",'
                '"memory_key":"learning.agent",'
                '"value":{"topic":"agent"},"scope_type":"global",'
                '"scope_id":null,"ttl_seconds":null}}'
            ),
        ),
    ]
    async with open_agent_client(migrated_database_path, outcomes) as (client, _):
        session = await client.post(
            "/api/v1/agent/sessions",
            json={"title": "拒绝测试"},
        )
        chatted = await client.post(
            f"/api/v1/agent/sessions/{session.json()['id']}/chat",
            json={"message": "我想学习 Agent"},
        )
        memory_id = chatted.json()["memory_candidate"]["id"]
        rejected = await client.post(
            f"/api/v1/memories/{memory_id}/reject",
            json={"reason": "not_useful"},
        )
        deleted = await client.delete(f"/api/v1/memories/{memory_id}")
        missing_body = await client.get(f"/api/v1/memories/{memory_id}")
        events = await client.get(f"/api/v1/memories/{memory_id}/events")

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert deleted.status_code == 200
    assert missing_body.status_code == 404
    assert [event["event_type"] for event in events.json()] == [
        "candidate_created",
        "rejected",
        "deleted",
    ]


async def test_missing_llm_key_keeps_session_api_but_chat_returns_503(
    migrated_database_path: Path,
) -> None:
    """离线面板无需模型 Key；只有真正聊天时才返回可理解的 503。"""

    settings = Settings(
        _env_file=None,
        database_path=migrated_database_path,
        market_data_mode="fake",
        llm_api_key=None,
    )  # type: ignore[call-arg]
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/v1/agent/sessions",
                json={"title": "离线会话"},
            )
            unavailable = await client.post(
                f"/api/v1/agent/sessions/{created.json()['id']}/chat",
                json={"message": "查询资产"},
            )
            missing = await client.get(
                "/api/v1/agent/sessions/00000000-0000-0000-0000-000000000000"
            )

    assert created.status_code == 201
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "AgentUnavailableError"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ConversationNotFoundError"
