"""Phase 8 会话/记忆 Repository 与 Unit of Work 集成测试。

测试统一使用 Alembic 创建 pytest 临时 SQLite，不读取个人资产数据库，也不调用模型或行情
接口。重点验证消息顺序、JSON 往返、事务原子性、ACTIVE 身份冲突和删除后的审计保留。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config

from finagent.llm import Message, MessageRole, ToolCall
from finagent.memory import (
    ConversationArchivedError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationSession,
    ConversationStatus,
    MemoryActor,
    MemoryConflictError,
    MemoryEvent,
    MemoryEventType,
    MemoryItem,
    MemoryItemNotFoundError,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
)
from finagent.persistence import DatabaseManager
from finagent.persistence.memory_unit_of_work import SqlAlchemyMemoryUnitOfWorkFactory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


@pytest.fixture
def migrated_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """返回已迁移到最新 revision 的隔离 SQLite 文件。"""

    database_path = tmp_path / "memory.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return database_path


def _session(*, title: str = "Phase 8 记忆测试") -> ConversationSession:
    """创建时间确定的有效会话，避免测试依赖系统当前时间。"""

    return ConversationSession(
        id=uuid4(),
        title=title,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_conversation_repository_persists_ordered_tool_messages(
    migrated_database_path: Path,
) -> None:
    """仓库应自动编号并完整恢复 assistant 工具请求和 tool 结果。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session = _session()

    try:
        async with factory() as unit_of_work:
            await unit_of_work.conversations.add_session(session)
            user_message = await unit_of_work.conversations.append_message(
                session.id,
                Message(role=MessageRole.USER, content="我的黄金仓位上限是 30%"),
                created_at=NOW + timedelta(seconds=1),
            )
            assistant_message = await unit_of_work.conversations.append_message(
                session.id,
                Message(
                    role=MessageRole.ASSISTANT,
                    tool_calls=(
                        ToolCall(
                            id="call-portfolio-1",
                            name="get_portfolio_summary",
                            arguments={"include_prices": True},
                        ),
                    ),
                ),
                created_at=NOW + timedelta(seconds=2),
            )
            tool_message = await unit_of_work.conversations.append_message(
                session.id,
                Message(
                    role=MessageRole.TOOL,
                    content='{"total_market_value":"10000.00"}',
                    tool_call_id="call-portfolio-1",
                ),
                created_at=NOW + timedelta(seconds=3),
            )
            await unit_of_work.commit()

        # 使用新的工作单元模拟应用下一次请求，确认数据来自 SQLite 而不是 Python 对象。
        async with factory() as unit_of_work:
            messages = await unit_of_work.conversations.list_messages(session.id)
            window = await unit_of_work.conversations.list_messages(
                session.id,
                after_sequence=1,
                limit=1,
            )
            persisted_session = await unit_of_work.conversations.get_session(session.id)

        assert [message.sequence_number for message in messages] == [1, 2, 3]
        assert messages[0] == user_message
        assert messages[1] == assistant_message
        assert messages[1].tool_calls[0].arguments == {"include_prices": True}
        assert messages[2] == tool_message
        assert window == (assistant_message,)
        assert persisted_session.updated_at == tool_message.created_at
    finally:
        await factory.close()


async def test_conversation_crud_archive_and_validation(
    migrated_database_path: Path,
) -> None:
    """会话应支持更新、筛选、删除，并拒绝归档后追加和非法窗口参数。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session = _session()
    updated = ConversationSession.model_validate(
        {
            **session.model_dump(),
            "title": "已完成风险偏好确认",
            "status": ConversationStatus.ARCHIVED,
            "summary": "用户希望黄金仓位不超过 30%。",
            "summary_until_sequence": 1,
            "updated_at": NOW + timedelta(minutes=1),
        }
    )

    try:
        async with factory() as unit_of_work:
            await unit_of_work.conversations.add_session(session)
            await unit_of_work.conversations.append_message(
                session.id,
                Message(role=MessageRole.USER, content="黄金仓位不要超过 30%"),
                created_at=NOW + timedelta(seconds=1),
            )
            await unit_of_work.conversations.update_session(updated)

            archived = await unit_of_work.conversations.list_sessions(
                ConversationStatus.ARCHIVED
            )
            assert archived == (updated,)
            with pytest.raises(ConversationArchivedError, match="已归档"):
                await unit_of_work.conversations.append_message(
                    session.id,
                    Message(role=MessageRole.USER, content="继续聊"),
                    created_at=NOW + timedelta(minutes=2),
                )
            with pytest.raises(ValueError, match="after_sequence"):
                await unit_of_work.conversations.list_messages(
                    session.id,
                    after_sequence=-1,
                )
            await unit_of_work.commit()

        async with factory() as unit_of_work:
            deleted = await unit_of_work.conversations.delete_session(session.id)
            assert deleted == updated
            await unit_of_work.commit()

        async with factory() as unit_of_work:
            with pytest.raises(ConversationNotFoundError):
                await unit_of_work.conversations.get_session(session.id)
    finally:
        await factory.close()


async def test_conversation_repository_protects_immutable_and_time_order(
    migrated_database_path: Path,
) -> None:
    """创建时间和消息时间线不能倒退，防止摘要窗口与消息顺序失真。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session = _session()

    try:
        async with factory() as unit_of_work:
            await unit_of_work.conversations.add_session(session)
            changed_created_at = ConversationSession.model_validate(
                {
                    **session.model_dump(),
                    "created_at": NOW - timedelta(days=1),
                }
            )
            with pytest.raises(ConversationConflictError, match="创建时间"):
                await unit_of_work.conversations.update_session(changed_created_at)
            with pytest.raises(ConversationConflictError, match="新消息时间"):
                await unit_of_work.conversations.append_message(
                    session.id,
                    Message(role=MessageRole.USER, content="倒序消息"),
                    created_at=NOW - timedelta(seconds=1),
                )
    finally:
        await factory.close()


async def test_memory_repository_lifecycle_filters_and_audit_survival(
    migrated_database_path: Path,
) -> None:
    """候选确认、条件查询、来源解绑和硬删除后保留审计应形成完整闭环。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session = _session()
    memory_created_at = NOW + timedelta(seconds=2)

    try:
        async with factory() as unit_of_work:
            await unit_of_work.conversations.add_session(session)
            source_message = await unit_of_work.conversations.append_message(
                session.id,
                Message(role=MessageRole.USER, content="黄金仓位上限设置为 30%"),
                created_at=NOW + timedelta(seconds=1),
            )
            candidate = MemoryItem(
                memory_type=MemoryType.CONSTRAINT,
                memory_key="max_position_percent",
                value={"percent": "30", "reason": "控制单一资产风险"},
                scope_type=MemoryScopeType.ASSET,
                scope_id="jd-zs-gold",
                source_session_id=session.id,
                source_message_id=source_message.id,
                created_at=memory_created_at,
                updated_at=memory_created_at,
            )
            await unit_of_work.memories.add_memory(candidate)
            await unit_of_work.memories.add_event(
                MemoryEvent(
                    memory_id=candidate.id,
                    event_type=MemoryEventType.CANDIDATE_CREATED,
                    actor=MemoryActor.MODEL,
                    details={"version": 1},
                    occurred_at=memory_created_at,
                )
            )
            await unit_of_work.commit()

        confirmed_at = NOW + timedelta(seconds=3)
        active = MemoryItem.model_validate(
            {
                **candidate.model_dump(),
                "status": MemoryStatus.ACTIVE,
                "confirmed_at": confirmed_at,
                "updated_at": confirmed_at,
            }
        )
        async with factory() as unit_of_work:
            await unit_of_work.memories.update_memory(active)
            await unit_of_work.memories.add_event(
                MemoryEvent(
                    memory_id=active.id,
                    event_type=MemoryEventType.CONFIRMED,
                    actor=MemoryActor.USER,
                    details={"version": 1},
                    occurred_at=confirmed_at,
                )
            )
            await unit_of_work.commit()

        async with factory() as unit_of_work:
            selected = await unit_of_work.memories.list_memories(
                status=MemoryStatus.ACTIVE,
                memory_type=MemoryType.CONSTRAINT,
                scope_type=MemoryScopeType.ASSET,
                scope_id="jd-zs-gold",
            )
            events = await unit_of_work.memories.list_events(active.id)
        assert selected == (active,)
        assert [event.event_type for event in events] == [
            MemoryEventType.CANDIDATE_CREATED,
            MemoryEventType.CONFIRMED,
        ]

        # 删除来源会话会级联清除短期消息，但已确认长期记忆只把来源外键置空。
        async with factory() as unit_of_work:
            await unit_of_work.conversations.delete_session(session.id)
            await unit_of_work.commit()
        async with factory() as unit_of_work:
            detached = await unit_of_work.memories.get_memory(active.id)
        assert detached.source_session_id is None
        assert detached.source_message_id is None

        deleted_at = NOW + timedelta(seconds=4)
        async with factory() as unit_of_work:
            await unit_of_work.memories.add_event(
                MemoryEvent(
                    memory_id=active.id,
                    event_type=MemoryEventType.DELETED,
                    actor=MemoryActor.USER,
                    details={"reason": "user_request"},
                    occurred_at=deleted_at,
                )
            )
            await unit_of_work.memories.delete_memory(active.id)
            await unit_of_work.commit()

        async with factory() as unit_of_work:
            with pytest.raises(MemoryItemNotFoundError):
                await unit_of_work.memories.get_memory(active.id)
            surviving_events = await unit_of_work.memories.list_events(active.id)
        assert surviving_events[-1].event_type is MemoryEventType.DELETED
        assert surviving_events[-1].details == {"reason": "user_request"}
    finally:
        await factory.close()


async def test_memory_repository_rejects_duplicate_active_identity(
    migrated_database_path: Path,
) -> None:
    """同一类型、键和范围只能有一条 ACTIVE 记忆，候选记录不受此限制。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()

    def active_memory(memory_id: UUID) -> MemoryItem:
        """创建身份相同但正文不同的 ACTIVE 记忆。"""

        return MemoryItem(
            id=memory_id,
            memory_type=MemoryType.PREFERENCE,
            memory_key="risk_level",
            value={"level": "low"},
            status=MemoryStatus.ACTIVE,
            confirmed_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )

    first = active_memory(uuid4())
    second = active_memory(uuid4())
    try:
        async with factory() as unit_of_work:
            await unit_of_work.memories.add_memory(first)
            await unit_of_work.commit()

        with pytest.raises(MemoryConflictError, match="写入冲突"):
            async with factory() as unit_of_work:
                await unit_of_work.memories.add_memory(second)

        async with factory() as unit_of_work:
            assert await unit_of_work.memories.list_memories(
                status=MemoryStatus.ACTIVE,
                scope_type=MemoryScopeType.GLOBAL,
            ) == (first,)
            with pytest.raises(ValueError, match="必须同时提供"):
                await unit_of_work.memories.list_memories(scope_id="017811")
    finally:
        await factory.close()


async def test_memory_unit_of_work_rolls_back_all_repositories_together(
    migrated_database_path: Path,
) -> None:
    """消息、候选正文和事件任一步失败时，整笔操作都不能留下半成品。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session = _session()
    memory_id = uuid4()

    try:
        with pytest.raises(RuntimeError, match="模拟候选提取后失败"):
            async with factory() as unit_of_work:
                await unit_of_work.conversations.add_session(session)
                source_message = await unit_of_work.conversations.append_message(
                    session.id,
                    Message(role=MessageRole.USER, content="请记住我偏好低风险"),
                    created_at=NOW + timedelta(seconds=1),
                )
                await unit_of_work.memories.add_memory(
                    MemoryItem(
                        id=memory_id,
                        memory_type=MemoryType.PREFERENCE,
                        memory_key="risk_level",
                        value={"level": "low"},
                        source_session_id=session.id,
                        source_message_id=source_message.id,
                        created_at=NOW + timedelta(seconds=2),
                        updated_at=NOW + timedelta(seconds=2),
                    )
                )
                await unit_of_work.memories.add_event(
                    MemoryEvent(
                        memory_id=memory_id,
                        event_type=MemoryEventType.CANDIDATE_CREATED,
                        actor=MemoryActor.MODEL,
                        occurred_at=NOW + timedelta(seconds=2),
                    )
                )
                raise RuntimeError("模拟候选提取后失败")

        async with factory() as unit_of_work:
            assert await unit_of_work.conversations.list_sessions() == ()
            assert await unit_of_work.memories.list_memories() == ()
            assert await unit_of_work.memories.list_events(memory_id) == ()
    finally:
        await factory.close()


async def test_memory_unit_of_work_requires_context_manager(
    migrated_database_path: Path,
) -> None:
    """工作单元进入前和退出后不能泄露已关闭的 Repository 或 Session。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    unit_of_work = factory()
    with pytest.raises(RuntimeError, match="尚未进入"):
        _ = unit_of_work.conversations
    with pytest.raises(RuntimeError, match="尚未进入"):
        await unit_of_work.commit()

    async with unit_of_work:
        pass

    with pytest.raises(RuntimeError, match="尚未进入"):
        _ = unit_of_work.memories
    await factory.close()
