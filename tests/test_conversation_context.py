"""持久化会话、滚动摘要与 Agent 上下文组装的离线测试。

测试使用临时 SQLite、可控时钟、Fake Summarizer 和 Fake ModelProvider。重点防止摘要重复注入、
工具链被截断、候选记忆进入上下文，以及模型调用期间长期占用数据库事务。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config

from finagent.llm import (
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from finagent.memory import (
    ContextAssembler,
    ConversationArchivedError,
    ConversationHistoryError,
    ConversationMessage,
    ConversationService,
    ConversationSession,
    ConversationStatus,
    ConversationSummaryConflictError,
    ConversationSummaryError,
    MemoryCandidateCreate,
    MemoryScopeType,
    MemoryService,
    MemoryType,
    ModelConversationSummarizer,
)
from finagent.persistence import DatabaseManager
from finagent.persistence.memory_unit_of_work import SqlAlchemyMemoryUnitOfWorkFactory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


class MutableClock:
    """由测试显式推进的服务端时钟。"""

    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        """返回当前测试时间。"""

        return self.current

    def advance(self, *, seconds: int = 1) -> None:
        """推进时间，保证连续持久化消息不会出现倒序。"""

        self.current += timedelta(seconds=seconds)


class RecordingSummarizer:
    """记录每次摘要输入，并按消息序号生成确定结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, tuple[ConversationMessage, ...]]] = []

    async def summarize(
        self,
        previous_summary: str | None,
        messages: tuple[ConversationMessage, ...],
    ) -> str:
        """返回能显示滚动覆盖范围的固定摘要。"""

        self.calls.append((previous_summary, messages))
        segment = ",".join(str(message.sequence_number) for message in messages)
        return f"{previous_summary + '；' if previous_summary else ''}已摘要消息 {segment}"


class SequenceProvider:
    """按顺序返回摘要响应，并记录统一模型请求。"""

    def __init__(self, outcomes: list[ModelResponse]) -> None:
        self._outcomes = iter(outcomes)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """记录请求并返回下一预设结果。"""

        self.requests.append(request)
        return next(self._outcomes)

    async def close(self) -> None:
        """Fake Provider 没有网络资源需要释放。"""


@pytest.fixture
def migrated_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """返回迁移到最新 revision 的一次性 SQLite。"""

    database_path = tmp_path / "conversation-context.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return database_path


async def _append_turn(
    service: ConversationService,
    clock: MutableClock,
    session_id: UUID,
    number: int,
) -> tuple[ConversationMessage, ConversationMessage]:
    """追加一轮不含工具调用的 user/assistant 消息。"""

    clock.advance()
    user = await service.append_message(
        session_id,
        Message(role=MessageRole.USER, content=f"问题 {number}"),
    )
    clock.advance()
    assistant = await service.append_message(
        session_id,
        Message(role=MessageRole.ASSISTANT, content=f"回答 {number}"),
    )
    return user, assistant


async def test_conversation_service_crud_persists_across_restart(
    migrated_database_path: Path,
) -> None:
    """会话、消息和归档状态应在重建 DatabaseManager 后继续存在。"""

    first_factory = SqlAlchemyMemoryUnitOfWorkFactory(
        DatabaseManager(migrated_database_path)
    )
    await first_factory.initialize()
    clock = MutableClock()
    summarizer = RecordingSummarizer()
    service = ConversationService(first_factory, summarizer, clock=clock)

    session = await service.create_session("  第一段持久化会话  ")
    await _append_turn(service, clock, session.id, 1)
    clock.advance()
    archived = await service.archive_session(session.id)
    assert archived.status is ConversationStatus.ARCHIVED
    with pytest.raises(ConversationArchivedError):
        await service.append_message(
            session.id,
            Message(role=MessageRole.USER, content="归档后继续输入"),
        )
    await first_factory.close()

    restarted_factory = SqlAlchemyMemoryUnitOfWorkFactory(
        DatabaseManager(migrated_database_path)
    )
    restarted_service = ConversationService(
        restarted_factory,
        RecordingSummarizer(),
        clock=clock,
    )
    try:
        await restarted_factory.initialize()
        restored = await restarted_service.get_session(session.id)
        window = await restarted_service.load_window(session.id)
        assert restored == archived
        assert restored.title == "第一段持久化会话"
        assert [message.content for message in window.recent_messages] == [
            "问题 1",
            "回答 1",
        ]
        assert await restarted_service.list_sessions(ConversationStatus.ARCHIVED) == (
            restored,
        )
        assert await restarted_service.delete_session(session.id) == restored
        assert await restarted_service.list_sessions() == ()
    finally:
        await restarted_factory.close()


async def test_rolling_summary_advances_without_deleting_or_repeating_messages(
    migrated_database_path: Path,
) -> None:
    """每次只摘要最老完整轮次，旧摘要作为输入累积，原始消息仍完整保留。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    clock = MutableClock()
    summarizer = RecordingSummarizer()
    service = ConversationService(
        factory,
        summarizer,
        recent_message_limit=4,
        clock=clock,
    )
    memory_service = MemoryService(factory, clock=clock)
    assembler = ContextAssembler(service, memory_service)

    try:
        session = await service.create_session("滚动摘要")
        for turn_number in range(1, 4):
            await _append_turn(service, clock, session.id, turn_number)

        clock.advance()
        first_summary = await service.refresh_summary(session.id)
        assert first_summary.summary_until_sequence == 2
        assert [message.sequence_number for message in summarizer.calls[0][1]] == [1, 2]
        assert summarizer.calls[0][0] is None

        await _append_turn(service, clock, session.id, 4)
        clock.advance()
        second_summary = await service.refresh_summary(session.id)
        assert second_summary.summary_until_sequence == 4
        assert summarizer.calls[1][0] == first_summary.summary
        assert [message.sequence_number for message in summarizer.calls[1][1]] == [3, 4]

        window = await service.load_window(session.id)
        assert [message.sequence_number for message in window.recent_messages] == [5, 6, 7, 8]
        async with factory() as unit_of_work:
            complete_history = await unit_of_work.conversations.list_messages(session.id)
        assert [message.sequence_number for message in complete_history] == list(range(1, 9))

        context = await assembler.assemble(
            session.id,
            system_prompt="你是测试投资助手",
        )
        assert context.session.summary_until_sequence == 4
        assert context.messages[1].role is MessageRole.SYSTEM
        assert "conversation_summary" in (context.messages[1].content or "")
        assert [message.content for message in context.messages[2:]] == [
            "问题 3",
            "回答 3",
            "问题 4",
            "回答 4",
        ]
        assert len(summarizer.calls) == 2
    finally:
        await factory.close()


async def test_summary_never_splits_assistant_tool_chain(
    migrated_database_path: Path,
) -> None:
    """软窗口不足以容纳完整工具轮次时宁可超限，也不能留下孤立 tool 结果。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    clock = MutableClock()
    summarizer = RecordingSummarizer()
    service = ConversationService(
        factory,
        summarizer,
        recent_message_limit=3,
        clock=clock,
    )

    try:
        session = await service.create_session("工具链摘要边界")
        clock.advance()
        await service.append_message(
            session.id,
            Message(role=MessageRole.USER, content="查询组合"),
        )
        clock.advance()
        await service.append_message(
            session.id,
            Message(
                role=MessageRole.ASSISTANT,
                tool_calls=(
                    ToolCall(id="call-1", name="get_portfolio_summary", arguments={}),
                ),
            ),
        )
        clock.advance()
        await service.append_message(
            session.id,
            Message(
                role=MessageRole.TOOL,
                content='{"ok":true}',
                tool_call_id="call-1",
            ),
        )
        clock.advance()
        await service.append_message(
            session.id,
            Message(role=MessageRole.ASSISTANT, content="组合查询完成"),
        )
        await _append_turn(service, clock, session.id, 2)

        clock.advance()
        unchanged = await service.refresh_summary(session.id)
        assert unchanged.summary is None
        assert summarizer.calls == []
        assert len((await service.load_window(session.id)).recent_messages) == 6

        smaller_window_service = ConversationService(
            factory,
            summarizer,
            recent_message_limit=2,
            clock=clock,
        )
        clock.advance()
        summarized = await smaller_window_service.refresh_summary(session.id)
        assert summarized.summary_until_sequence == 4
        assert [message.sequence_number for message in summarizer.calls[0][1]] == [1, 2, 3, 4]
        assert [
            message.sequence_number
            for message in (await smaller_window_service.load_window(session.id)).recent_messages
        ] == [5, 6]
    finally:
        await factory.close()


async def test_context_injects_only_global_and_requested_asset_active_memories(
    migrated_database_path: Path,
) -> None:
    """上下文应排除候选、过期和无关资产记忆，并保持长期记忆为独立数据块。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    clock = MutableClock()
    conversation_service = ConversationService(
        factory,
        RecordingSummarizer(),
        clock=clock,
    )
    memory_service = MemoryService(factory, clock=clock)
    assembler = ContextAssembler(conversation_service, memory_service)

    try:
        session = await conversation_service.create_session("上下文记忆过滤")
        clock.advance()
        source = await conversation_service.append_message(
            session.id,
            Message(role=MessageRole.USER, content="请记住这些投资偏好"),
        )

        async def create_and_confirm(
            *,
            memory_type: MemoryType,
            memory_key: str,
            value: dict[str, object],
            scope_type: MemoryScopeType = MemoryScopeType.GLOBAL,
            scope_id: str | None = None,
            ttl_seconds: int | None = None,
        ) -> UUID:
            """创建并确认测试长期记忆，返回记忆 UUID。"""

            candidate = await memory_service.create_candidate(
                MemoryCandidateCreate.model_validate(
                    {
                        "memory_type": memory_type,
                        "memory_key": memory_key,
                        "value": value,
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "source_session_id": session.id,
                        "source_message_id": source.id,
                        "ttl_seconds": ttl_seconds,
                    }
                )
            )
            clock.advance()
            return (await memory_service.confirm_candidate(candidate.id)).memory.id

        await create_and_confirm(
            memory_type=MemoryType.PREFERENCE,
            memory_key="risk_level",
            value={"level": "low"},
        )
        await create_and_confirm(
            memory_type=MemoryType.CONSTRAINT,
            memory_key="max_position_percent",
            value={"percent": "20"},
            scope_type=MemoryScopeType.ASSET,
            scope_id="017811",
        )
        await create_and_confirm(
            memory_type=MemoryType.CONSTRAINT,
            memory_key="max_position_percent",
            value={"percent": "10"},
            scope_type=MemoryScopeType.ASSET,
            scope_id="JD-ZS-GOLD",
        )
        await create_and_confirm(
            memory_type=MemoryType.GOAL,
            memory_key="temporary_goal",
            value={"goal": "observe"},
            ttl_seconds=2,
        )
        clock.advance()
        # 该候选故意不确认，用于验证 CANDIDATE 不会进入上下文。
        await memory_service.create_candidate(
            MemoryCandidateCreate(
                memory_type=MemoryType.FEEDBACK,
                memory_key="answer_style",
                value={"style": "concise"},
                source_session_id=session.id,
                source_message_id=source.id,
            )
        )

        context = await assembler.assemble(
            session.id,
            system_prompt="你是测试投资助手",
            asset_symbols=("017811",),
        )
        assert len(context.active_memories) == 2
        assert {(memory.memory_key, memory.scope_id) for memory in context.active_memories} == {
            ("risk_level", None),
            ("max_position_percent", "017811"),
        }
        memory_block = context.messages[1].content or ""
        assert "confirmed_memories" in memory_block
        assert '"level":"low"' in memory_block
        assert '"percent":"20"' in memory_block
        assert '"percent":"10"' not in memory_block
        assert "answer_style" not in memory_block
        assert "temporary_goal" not in memory_block
        assert context.messages[-1].role is MessageRole.USER
    finally:
        await factory.close()


async def test_model_summarizer_uses_provider_without_tools() -> None:
    """真实摘要适配器应使用统一模型接口、零温度并拒绝模型返回工具调用。"""

    provider = SequenceProvider(
        [
            ModelResponse(model="fake-summary", content="用户关注黄金风险。"),
            ModelResponse(
                model="fake-summary",
                tool_calls=(ToolCall(id="call-1", name="unexpected_tool", arguments={}),),
            ),
        ]
    )
    summarizer = ModelConversationSummarizer(provider, max_output_tokens=500)
    session_id = ConversationSession(
        title="模型摘要",
        created_at=NOW,
        updated_at=NOW,
    ).id
    messages = (
        ConversationMessage(
            session_id=session_id,
            sequence_number=1,
            role=MessageRole.USER,
            content="关注黄金风险",
            created_at=NOW,
        ),
        ConversationMessage(
            session_id=session_id,
            sequence_number=2,
            role=MessageRole.ASSISTANT,
            content="已经记录本轮上下文",
            created_at=NOW,
        ),
    )

    summary = await summarizer.summarize("旧摘要", messages)
    assert summary == "用户关注黄金风险。"
    request = provider.requests[0]
    assert request.tool_choice == "none"
    assert request.temperature == 0
    assert request.max_output_tokens == 500
    payload = json.loads(request.messages[1].content or "{}")
    assert payload["previous_summary"] == "旧摘要"
    assert [item["sequence_number"] for item in payload["new_messages"]] == [1, 2]

    with pytest.raises(ConversationSummaryError, match="不允许返回工具调用"):
        await summarizer.summarize(None, messages)


async def test_summary_write_detects_concurrent_summary_change(
    migrated_database_path: Path,
) -> None:
    """摘要模型运行期间若另一任务推进摘要，旧结果不能覆盖新状态。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    clock = MutableClock()

    class ConflictingSummarizer:
        """在返回摘要前模拟另一个任务已经写入摘要。"""

        def __init__(self, target_session_id: UUID) -> None:
            self._target_session_id = target_session_id

        async def summarize(
            self,
            previous_summary: str | None,
            messages: tuple[ConversationMessage, ...],
        ) -> str:
            """并发推进摘要到同一完整轮次，再返回已经过期的计算结果。"""

            clock.advance()
            async with factory() as unit_of_work:
                session = await unit_of_work.conversations.get_session(
                    self._target_session_id
                )
                concurrent = ConversationSession.model_validate(
                    {
                        **session.model_dump(),
                        "summary": "并发任务生成的新摘要",
                        "summary_until_sequence": messages[-1].sequence_number,
                        "updated_at": clock.current,
                    }
                )
                await unit_of_work.conversations.update_session(concurrent)
                await unit_of_work.commit()
            return "较慢任务生成的旧摘要"

    try:
        bootstrap = ConversationService(
            factory,
            RecordingSummarizer(),
            recent_message_limit=2,
            clock=clock,
        )
        session = await bootstrap.create_session("摘要并发")
        for number in range(1, 3):
            await _append_turn(bootstrap, clock, session.id, number)

        service = ConversationService(
            factory,
            ConflictingSummarizer(session.id),
            recent_message_limit=2,
            clock=clock,
        )
        with pytest.raises(ConversationSummaryConflictError):
            await service.refresh_summary(session.id)
        persisted = await service.get_session(session.id)
        assert persisted.summary == "并发任务生成的新摘要"
        assert persisted.summary_until_sequence == 2
    finally:
        await factory.close()


async def test_conversation_history_rejects_system_and_orphan_tool(
    migrated_database_path: Path,
) -> None:
    """system prompt 不进入历史，绕过 Service 写入的孤立 tool 会在组装前被发现。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    clock = MutableClock()
    service = ConversationService(factory, RecordingSummarizer(), clock=clock)

    try:
        session = await service.create_session("非法历史")
        with pytest.raises(ConversationHistoryError, match="system prompt"):
            await service.append_message(
                session.id,
                Message(role=MessageRole.SYSTEM, content="不应持久化"),
            )

        # Repository 只保证单条消息结构；这里模拟其他代码绕过 Service 写入合法但顺序孤立的 tool。
        clock.advance()
        async with factory() as unit_of_work:
            await unit_of_work.conversations.append_message(
                session.id,
                Message(
                    role=MessageRole.TOOL,
                    content='{"ok":true}',
                    tool_call_id="missing-call",
                ),
                created_at=clock.current,
            )
            await unit_of_work.commit()

        with pytest.raises(ConversationHistoryError, match="必须从 user"):
            await service.load_window(session.id)
    finally:
        await factory.close()
