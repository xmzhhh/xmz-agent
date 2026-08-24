"""持久化 Agent 编排、完整轮次事务和故障回滚的离线测试。

所有模型响应和投资工具都使用本地 Fake，不访问百炼、AKShare 或 GoldAPI。测试重点是：
Agent 能从 SQLite 恢复上下文；成功工具链完整落库；模型失败、步数超限和数据库写入失败
不会留下半轮历史；工具参数错误则允许模型纠正并保留可审计轨迹。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config

from finagent.agents import AgentStepLimitError, PersistentToolCallingAgent
from finagent.llm import (
    Message,
    MessageRole,
    ModelConnectionError,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from finagent.memory import (
    ContextAssembler,
    ConversationConflictError,
    ConversationService,
    MemoryService,
)
from finagent.memory.models import ConversationMessage
from finagent.persistence import DatabaseManager
from finagent.persistence.memory_repositories import SqlAlchemyConversationRepository
from finagent.persistence.memory_unit_of_work import SqlAlchemyMemoryUnitOfWorkFactory
from finagent.tools import PositionRatioTool, ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


class MutableClock:
    """返回带时区的确定时间，并允许测试模拟后续请求。"""

    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        """返回当前测试时间。"""

        return self.current

    def advance(self) -> None:
        """推进一秒，模拟下一次独立业务操作。"""

        self.current += timedelta(seconds=1)


class NoopSummarizer:
    """短消息场景不会实际调用的离线摘要器。"""

    async def summarize(
        self,
        previous_summary: str | None,
        messages: tuple[ConversationMessage, ...],
    ) -> str:
        """若测试意外触发摘要则立即失败，防止掩盖上下文窗口配置错误。"""

        raise AssertionError("本测试不应触发滚动摘要")


class SequenceProvider:
    """按顺序返回预设模型响应或异常，并保存每次完整请求。"""

    def __init__(self, outcomes: list[ModelResponse | Exception]) -> None:
        self._outcomes = iter(outcomes)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """记录请求后返回下一项；异常用于模拟网络或模型故障。"""

        self.requests.append(request)
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        """记录 Provider 已由 Agent 生命周期正确关闭。"""

        self.closed = True


@pytest.fixture
def migrated_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """创建迁移到最新 revision 的一次性 SQLite 文件。"""

    database_path = tmp_path / "persistent-agent.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return database_path


def _build_services(
    database_path: Path,
    clock: MutableClock,
) -> tuple[
    SqlAlchemyMemoryUnitOfWorkFactory,
    ConversationService,
    ContextAssembler,
]:
    """用同一个数据库管理器组合会话、记忆和上下文服务。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(database_path))
    conversation_service = ConversationService(
        factory,
        NoopSummarizer(),
        clock=clock,
    )
    assembler = ContextAssembler(
        conversation_service,
        MemoryService(factory, clock=clock),
    )
    return factory, conversation_service, assembler


def _registry() -> ToolRegistry:
    """只开放确定性仓位计算工具，避免测试产生外部请求或业务写操作。"""

    return ToolRegistry((PositionRatioTool(),))


async def test_successful_turn_is_restored_after_application_restart(
    migrated_database_path: Path,
) -> None:
    """进程重启后，新 Agent 应把上一轮 SQLite 历史注入下一次模型请求。"""

    clock = MutableClock()
    first_factory, first_service, first_assembler = _build_services(
        migrated_database_path,
        clock,
    )
    await first_factory.initialize()
    first_provider = SequenceProvider(
        [ModelResponse(model="fake-model", content="第一轮回答")]
    )
    first_agent = PersistentToolCallingAgent(
        first_provider,
        _registry(),
        first_assembler,
        first_service,
        "你是测试投资助手",
    )
    session = await first_service.create_session("可恢复 Agent")
    first_result = await first_agent.ask(session.id, " 第一轮问题 ")
    assert first_result.answer == "第一轮回答"
    assert [message.role for message in first_result.messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    await first_factory.close()

    # 重新创建 DatabaseManager、Service 和 Agent，模拟应用进程完全重启。
    clock.advance()
    second_factory, second_service, second_assembler = _build_services(
        migrated_database_path,
        clock,
    )
    second_provider = SequenceProvider(
        [ModelResponse(model="fake-model", content="第二轮回答")]
    )
    second_agent = PersistentToolCallingAgent(
        second_provider,
        _registry(),
        second_assembler,
        second_service,
        "你是测试投资助手",
    )
    try:
        await second_factory.initialize()
        second_result = await second_agent.ask(session.id, "第二轮问题")

        assert second_result.answer == "第二轮回答"
        assert [message.content for message in second_provider.requests[0].messages] == [
            "你是测试投资助手",
            "第一轮问题",
            "第一轮回答",
            "第二轮问题",
        ]
        restored = await second_service.load_window(session.id)
        assert [message.content for message in restored.recent_messages] == [
            "第一轮问题",
            "第一轮回答",
            "第二轮问题",
            "第二轮回答",
        ]
    finally:
        await second_factory.close()


async def test_successful_tool_loop_is_committed_as_one_complete_turn(
    migrated_database_path: Path,
) -> None:
    """工具请求、工具结果和最终回答应保持协议顺序并一次性写入 SQLite。"""

    factory, service, assembler = _build_services(migrated_database_path, MutableClock())
    await factory.initialize()
    tool_call = ToolCall(
        id="call-ratio",
        name="calculate_position_ratio",
        arguments={"position_value": 3000, "total_assets": 10000},
    )
    provider = SequenceProvider(
        [
            ModelResponse(model="fake-model", tool_calls=(tool_call,)),
            ModelResponse(model="fake-model", content="黄金仓位为 30%。"),
        ]
    )
    agent = PersistentToolCallingAgent(
        provider,
        _registry(),
        assembler,
        service,
        "你是测试投资助手",
    )
    try:
        session = await service.create_session("工具循环")
        result = await agent.ask(session.id, "计算黄金仓位")

        assert result.model_call_count == 2
        assert result.tool_call_names == ("calculate_position_ratio",)
        assert [message.role for message in result.messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.TOOL,
            MessageRole.ASSISTANT,
        ]
        assert result.messages[1].tool_calls == (tool_call,)
        assert result.messages[2].tool_call_id == "call-ratio"
        tool_content = result.messages[2].content
        assert tool_content is not None
        assert json.loads(tool_content)["data"]["position_ratio_percent"] == 30.0
        assert result.messages[-1].content == "黄金仓位为 30%。"
    finally:
        await factory.close()


async def test_tool_validation_error_is_persisted_only_after_model_recovers(
    migrated_database_path: Path,
) -> None:
    """非法参数可交给模型纠正；最终成功后错误轨迹才随整轮消息落库。"""

    factory, service, assembler = _build_services(migrated_database_path, MutableClock())
    await factory.initialize()
    invalid_call = ToolCall(
        id="call-invalid",
        name="calculate_position_ratio",
        arguments={"position_value": 12000, "total_assets": 10000},
    )
    provider = SequenceProvider(
        [
            ModelResponse(model="fake-model", tool_calls=(invalid_call,)),
            ModelResponse(model="fake-model", content="请检查持仓与总资产输入。"),
        ]
    )
    agent = PersistentToolCallingAgent(
        provider,
        _registry(),
        assembler,
        service,
        "你是测试投资助手",
    )
    try:
        session = await service.create_session("工具纠错")
        result = await agent.ask(session.id, "计算仓位")
        error_content = result.messages[2].content
        assert error_content is not None
        error_payload = json.loads(error_content)
        assert error_payload["ok"] is False
        assert error_payload["error_type"] == "ToolValidationError"
        assert "持仓市值不能大于账户总资产" in error_payload["error"]
    finally:
        await factory.close()


async def test_provider_failure_and_step_limit_leave_database_unchanged(
    migrated_database_path: Path,
) -> None:
    """模型中途失败或持续请求工具时，数据库都不能出现半轮 user/tool 消息。"""

    factory, service, assembler = _build_services(migrated_database_path, MutableClock())
    await factory.initialize()
    session = await service.create_session("失败回滚")
    tool_call = ToolCall(
        id="call-loop",
        name="calculate_position_ratio",
        arguments={"position_value": 1000, "total_assets": 10000},
    )
    failing_agent = PersistentToolCallingAgent(
        SequenceProvider(
            [
                ModelResponse(model="fake-model", tool_calls=(tool_call,)),
                ModelConnectionError("模拟第二次模型调用失败"),
            ]
        ),
        _registry(),
        assembler,
        service,
        "你是测试投资助手",
    )
    limited_agent = PersistentToolCallingAgent(
        SequenceProvider(
            [
                ModelResponse(model="fake-model", tool_calls=(tool_call,)),
                ModelResponse(model="fake-model", tool_calls=(tool_call,)),
            ]
        ),
        _registry(),
        assembler,
        service,
        "你是测试投资助手",
        max_steps=2,
    )
    try:
        with pytest.raises(ModelConnectionError, match="第二次模型调用失败"):
            await failing_agent.ask(session.id, "网络失败场景")
        assert (await service.load_window(session.id)).recent_messages == ()

        with pytest.raises(AgentStepLimitError, match="2 次模型调用"):
            await limited_agent.ask(session.id, "循环调用场景")
        assert (await service.load_window(session.id)).recent_messages == ()
    finally:
        await factory.close()


async def test_commit_turn_rolls_back_when_one_message_write_fails(
    migrated_database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整轮次中任一消息写入失败时，前面已经 flush 的消息也必须全部回滚。"""

    clock = MutableClock()
    factory, service, _ = _build_services(migrated_database_path, clock)
    await factory.initialize()
    session = await service.create_session("事务回滚")
    original_append = SqlAlchemyConversationRepository.append_message
    append_count = 0

    async def fail_on_third_message(
        repository: SqlAlchemyConversationRepository,
        session_id: UUID,
        message: Message,
        *,
        created_at: datetime,
    ) -> ConversationMessage:
        """在第三条消息处模拟数据库故障，验证 Unit of Work 自动回滚。"""

        nonlocal append_count
        append_count += 1
        if append_count == 3:
            raise RuntimeError("模拟第三条消息写入失败")
        return await original_append(
            repository,
            session_id,
            message,
            created_at=created_at,
        )

    monkeypatch.setattr(
        SqlAlchemyConversationRepository,
        "append_message",
        fail_on_third_message,
    )
    tool_call = ToolCall(
        id="call-rollback",
        name="calculate_position_ratio",
        arguments={"position_value": 1000, "total_assets": 10000},
    )
    messages = (
        Message(role=MessageRole.USER, content="测试事务"),
        Message(role=MessageRole.ASSISTANT, tool_calls=(tool_call,)),
        Message(role=MessageRole.TOOL, content='{"ok":true}', tool_call_id="call-rollback"),
        Message(role=MessageRole.ASSISTANT, content="最终回答"),
    )
    try:
        with pytest.raises(RuntimeError, match="第三条消息写入失败"):
            await service.commit_turn(
                session.id,
                messages,
                expected_session_updated_at=session.updated_at,
            )
        assert (await service.load_window(session.id)).recent_messages == ()
    finally:
        await factory.close()


async def test_commit_turn_rejects_result_built_from_stale_context(
    migrated_database_path: Path,
) -> None:
    """推理期间历史被其他请求推进后，旧上下文生成的回答不得继续追加。"""

    clock = MutableClock()
    factory, service, _ = _build_services(migrated_database_path, clock)
    await factory.initialize()
    session = await service.create_session("并发冲突")
    stale_updated_at = session.updated_at
    first_turn = (
        Message(role=MessageRole.USER, content="先完成的问题"),
        Message(role=MessageRole.ASSISTANT, content="先完成的回答"),
    )
    stale_turn = (
        Message(role=MessageRole.USER, content="基于旧上下文的问题"),
        Message(role=MessageRole.ASSISTANT, content="基于旧上下文的回答"),
    )
    try:
        clock.advance()
        await service.commit_turn(
            session.id,
            first_turn,
            expected_session_updated_at=stale_updated_at,
        )

        clock.advance()
        with pytest.raises(ConversationConflictError, match="会话已被其他请求更新"):
            await service.commit_turn(
                session.id,
                stale_turn,
                expected_session_updated_at=stale_updated_at,
            )

        restored = await service.load_window(session.id)
        assert [message.content for message in restored.recent_messages] == [
            "先完成的问题",
            "先完成的回答",
        ]
    finally:
        await factory.close()
