"""MemoryService 状态机、TTL、隐私过滤与原子替换测试。

所有测试使用 Alembic 创建一次性 SQLite，并注入固定时钟；不会调用大模型、行情接口或个人
数据库。测试中的敏感字段值只使用 ``<redacted>`` 占位符，不保存任何真实凭据。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from finagent.llm import Message, MessageRole
from finagent.memory import (
    ConversationSession,
    InvalidMemoryTransitionError,
    MemoryActor,
    MemoryAuditError,
    MemoryCandidateCreate,
    MemoryCandidateExpiredError,
    MemoryClockError,
    MemoryEventType,
    MemoryItem,
    MemoryItemNotFoundError,
    MemoryRejectionReason,
    MemoryScopeType,
    MemoryService,
    MemorySourceError,
    MemoryStatus,
    MemoryType,
    SensitiveMemoryError,
)
from finagent.memory.errors import MemoryConflictError
from finagent.persistence import DatabaseManager
from finagent.persistence.memory_repositories import SqlAlchemyMemoryRepository
from finagent.persistence.memory_unit_of_work import SqlAlchemyMemoryUnitOfWorkFactory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)


class MutableClock:
    """允许测试显式推进或倒退的带时区服务端时钟。"""

    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        """返回测试控制的当前时间。"""

        return self.current

    def advance(self, *, seconds: int) -> None:
        """按秒推进时钟，不进行真实等待。"""

        self.current += timedelta(seconds=seconds)


@pytest.fixture
def migrated_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """返回迁移到最新 revision 的隔离 SQLite 文件。"""

    database_path = tmp_path / "memory-service.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("MARKET_DATA_MODE", "fake")
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    return database_path


async def _create_source_message(
    factory: SqlAlchemyMemoryUnitOfWorkFactory,
    *,
    role: MessageRole = MessageRole.USER,
    content: str = "请把我的黄金仓位上限记为 30%",
) -> tuple[ConversationSession, UUID]:
    """在正式 Repository 中创建候选记忆可以追溯的会话消息。"""

    session = ConversationSession(
        title="MemoryService 测试会话",
        created_at=NOW - timedelta(seconds=2),
        updated_at=NOW - timedelta(seconds=2),
    )
    async with factory() as unit_of_work:
        await unit_of_work.conversations.add_session(session)
        message = await unit_of_work.conversations.append_message(
            session.id,
            Message(role=role, content=content),
            created_at=NOW - timedelta(seconds=1),
        )
        await unit_of_work.commit()
    return session, message.id


def _candidate_command(
    session: ConversationSession,
    message_id: UUID,
    *,
    value: dict[str, object] | None = None,
    ttl_seconds: int | None = None,
) -> MemoryCandidateCreate:
    """构造同一资产约束身份的严格候选命令。"""

    return MemoryCandidateCreate.model_validate(
        {
            "memory_type": MemoryType.CONSTRAINT,
            "memory_key": "max_position_percent",
            "value": value or {"percent": "30"},
            "scope_type": MemoryScopeType.ASSET,
            "scope_id": "jd-zs-gold",
            "source_session_id": session.id,
            "source_message_id": message_id,
            "ttl_seconds": ttl_seconds,
        }
    )


async def test_create_candidate_is_model_only_and_auditable(
    migrated_database_path: Path,
) -> None:
    """模型命令不能携带 ACTIVE 状态，成功创建后只得到 CANDIDATE 和最小事件。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session, message_id = await _create_source_message(factory)
    service = MemoryService(factory, clock=MutableClock())
    command_data = _candidate_command(session, message_id).model_dump(mode="json")

    try:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            MemoryCandidateCreate.model_validate({**command_data, "status": "active"})

        candidate = await service.create_candidate(_candidate_command(session, message_id))
        assert candidate.status is MemoryStatus.CANDIDATE
        assert candidate.confirmed_at is None
        assert candidate.scope_id == "JD-ZS-GOLD"

        async with factory() as unit_of_work:
            events = await unit_of_work.memories.list_events(candidate.id)
        assert len(events) == 1
        assert events[0].event_type is MemoryEventType.CANDIDATE_CREATED
        assert events[0].actor is MemoryActor.MODEL
        assert events[0].details == {"version": 1, "has_expiry": False}
        assert "percent" not in events[0].details
    finally:
        await factory.close()


async def test_create_candidate_rejects_sensitive_and_invalid_sources(
    migrated_database_path: Path,
) -> None:
    """凭据字段、assistant 来源和会话错配都必须在持久化候选前失败。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    user_session, user_message_id = await _create_source_message(factory)
    assistant_session, assistant_message_id = await _create_source_message(
        factory,
        role=MessageRole.ASSISTANT,
        content="我建议记住这条偏好。",
    )
    service = MemoryService(factory, clock=MutableClock())

    try:
        sensitive_key = _candidate_command(user_session, user_message_id).model_dump()
        sensitive_key["memory_key"] = "llm_api_key"
        with pytest.raises(SensitiveMemoryError) as key_error:
            await service.create_candidate(MemoryCandidateCreate.model_validate(sensitive_key))
        assert "<redacted>" not in str(key_error.value)

        with pytest.raises(SensitiveMemoryError):
            await service.create_candidate(
                _candidate_command(
                    user_session,
                    user_message_id,
                    value={"profile": {"password": "<redacted>"}},
                )
            )

        with pytest.raises(MemorySourceError, match="只能来源于 user"):
            await service.create_candidate(
                _candidate_command(assistant_session, assistant_message_id)
            )

        mismatched = _candidate_command(user_session, user_message_id).model_dump()
        mismatched["source_session_id"] = assistant_session.id
        with pytest.raises(MemorySourceError, match="不匹配"):
            await service.create_candidate(MemoryCandidateCreate.model_validate(mismatched))

        async with factory() as unit_of_work:
            assert await unit_of_work.memories.list_memories() == ()
    finally:
        await factory.close()


async def test_confirm_candidate_creates_active_memory(
    migrated_database_path: Path,
) -> None:
    """首次人工确认应产生 version 1 ACTIVE 记忆，重复确认必须失败。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session, message_id = await _create_source_message(factory)
    clock = MutableClock()
    service = MemoryService(factory, clock=clock)

    try:
        candidate = await service.create_candidate(_candidate_command(session, message_id))
        clock.advance(seconds=1)
        result = await service.confirm_candidate(candidate.id)

        assert result.memory.status is MemoryStatus.ACTIVE
        assert result.memory.version == 1
        assert result.memory.confirmed_at == clock.current
        assert result.superseded_memory is None
        assert await service.list_active_memories() == (result.memory,)

        with pytest.raises(InvalidMemoryTransitionError, match="只有 candidate"):
            await service.confirm_candidate(candidate.id)
    finally:
        await factory.close()


async def test_confirm_conflict_preserves_version_chain(
    migrated_database_path: Path,
) -> None:
    """确认同身份新候选时，旧 ACTIVE 必须变为 SUPERSEDED 并建立双向审计关联。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session, message_id = await _create_source_message(factory)
    clock = MutableClock()
    service = MemoryService(factory, clock=clock)

    try:
        first_candidate = await service.create_candidate(
            _candidate_command(session, message_id, value={"percent": "30"})
        )
        clock.advance(seconds=1)
        first_active = (await service.confirm_candidate(first_candidate.id)).memory

        clock.advance(seconds=1)
        second_candidate = await service.create_candidate(
            _candidate_command(session, message_id, value={"percent": "20"})
        )
        clock.advance(seconds=1)
        result = await service.confirm_candidate(second_candidate.id)

        assert result.memory.status is MemoryStatus.ACTIVE
        assert result.memory.version == 2
        assert result.memory.supersedes_id == first_active.id
        assert result.superseded_memory is not None
        assert result.superseded_memory.status is MemoryStatus.SUPERSEDED
        assert result.superseded_memory.value == {"percent": "30"}
        assert await service.list_active_memories() == (result.memory,)

        async with factory() as unit_of_work:
            old_events = await unit_of_work.memories.list_events(first_active.id)
            new_events = await unit_of_work.memories.list_events(result.memory.id)
        assert old_events[-1].details["replacement_memory_id"] == str(result.memory.id)
        assert new_events[-1].details["superseded_memory_id"] == str(first_active.id)
    finally:
        await factory.close()


async def test_conflict_replacement_rolls_back_if_new_activation_fails(
    migrated_database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧版本已写 SUPERSEDED 后若新版本激活失败，整个事务必须恢复原状。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session, message_id = await _create_source_message(factory)
    clock = MutableClock()
    service = MemoryService(factory, clock=clock)

    try:
        first_candidate = await service.create_candidate(_candidate_command(session, message_id))
        clock.advance(seconds=1)
        first_active = (await service.confirm_candidate(first_candidate.id)).memory
        clock.advance(seconds=1)
        second_candidate = await service.create_candidate(
            _candidate_command(session, message_id, value={"percent": "20"})
        )
        original_update = SqlAlchemyMemoryRepository.update_memory

        async def fail_new_activation(
            repository: SqlAlchemyMemoryRepository,
            memory: MemoryItem,
        ) -> MemoryItem:
            """只在第二候选变为 ACTIVE 时模拟数据库后续步骤失败。"""

            if memory.id == second_candidate.id and memory.status is MemoryStatus.ACTIVE:
                raise MemoryConflictError("模拟新版本激活失败")
            return await original_update(repository, memory)

        clock.advance(seconds=1)
        with monkeypatch.context() as patcher:
            patcher.setattr(SqlAlchemyMemoryRepository, "update_memory", fail_new_activation)
            with pytest.raises(MemoryConflictError, match="模拟新版本"):
                await service.confirm_candidate(second_candidate.id)

        async with factory() as unit_of_work:
            persisted_first = await unit_of_work.memories.get_memory(first_active.id)
            persisted_second = await unit_of_work.memories.get_memory(second_candidate.id)
            old_events = await unit_of_work.memories.list_events(first_active.id)
        assert persisted_first.status is MemoryStatus.ACTIVE
        assert persisted_second.status is MemoryStatus.CANDIDATE
        assert all(event.event_type is not MemoryEventType.SUPERSEDED for event in old_events)
    finally:
        await factory.close()


async def test_reject_candidate_is_terminal_and_uses_reason_code(
    migrated_database_path: Path,
) -> None:
    """用户拒绝候选后不能再次迁移，审计只保存枚举原因而非自由文本。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session, message_id = await _create_source_message(factory)
    clock = MutableClock()
    service = MemoryService(factory, clock=clock)

    try:
        candidate = await service.create_candidate(_candidate_command(session, message_id))
        clock.advance(seconds=1)
        rejected = await service.reject_candidate(
            candidate.id,
            reason=MemoryRejectionReason.INCORRECT,
        )
        assert rejected.status is MemoryStatus.REJECTED
        assert rejected.confirmed_at is None

        async with factory() as unit_of_work:
            events = await unit_of_work.memories.list_events(candidate.id)
        assert events[-1].details == {"version": 1, "reason_code": "incorrect"}

        with pytest.raises(InvalidMemoryTransitionError):
            await service.reject_candidate(candidate.id)
        with pytest.raises(InvalidMemoryTransitionError):
            await service.confirm_candidate(candidate.id)
    finally:
        await factory.close()


async def test_ttl_boundary_expires_active_and_blocks_stale_candidate(
    migrated_database_path: Path,
) -> None:
    """到期前仍可确认，到期时 ACTIVE 立即退出上下文，过期候选不能复活。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session, message_id = await _create_source_message(factory)
    clock = MutableClock()
    service = MemoryService(factory, clock=clock)

    try:
        candidate = await service.create_candidate(
            _candidate_command(session, message_id, ttl_seconds=900)
        )
        clock.advance(seconds=899)
        active = (await service.confirm_candidate(candidate.id)).memory
        assert await service.list_active_memories() == (active,)

        clock.advance(seconds=1)
        assert await service.list_active_memories() == ()
        assert await service.expire_due_memories() == ()
        async with factory() as unit_of_work:
            expired = await unit_of_work.memories.get_memory(active.id)
            events = await unit_of_work.memories.list_events(active.id)
        assert expired.status is MemoryStatus.EXPIRED
        assert events[-1].event_type is MemoryEventType.EXPIRED
        assert events[-1].actor is MemoryActor.SYSTEM

        stale_candidate = await service.create_candidate(
            _candidate_command(session, message_id, ttl_seconds=10)
        )
        clock.advance(seconds=10)
        with pytest.raises(MemoryCandidateExpiredError):
            await service.confirm_candidate(stale_candidate.id)

        # 即使上一版已经 EXPIRED，新确认版本也要沿用历史版本链，不能重置为 version 1。
        replacement = await service.create_candidate(
            _candidate_command(session, message_id, value={"percent": "20"})
        )
        clock.advance(seconds=1)
        replacement_active = (await service.confirm_candidate(replacement.id)).memory
        assert replacement_active.version == 2
        assert replacement_active.supersedes_id == active.id
    finally:
        await factory.close()


async def test_delete_removes_body_but_keeps_whitelisted_audit(
    migrated_database_path: Path,
) -> None:
    """硬删除后正文不可读取，删除事件只保留版本、旧状态和固定原因。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session, message_id = await _create_source_message(factory)
    clock = MutableClock()
    service = MemoryService(factory, clock=clock)

    try:
        candidate = await service.create_candidate(
            _candidate_command(session, message_id, value={"percent": "30"})
        )
        clock.advance(seconds=1)
        active = (await service.confirm_candidate(candidate.id)).memory
        clock.advance(seconds=1)
        deleted = await service.delete_memory(active.id)
        assert deleted == active

        async with factory() as unit_of_work:
            with pytest.raises(MemoryItemNotFoundError):
                await unit_of_work.memories.get_memory(active.id)
            events = await unit_of_work.memories.list_events(active.id)
        assert events[-1].event_type is MemoryEventType.DELETED
        assert events[-1].details == {
            "version": 1,
            "prior_status": "active",
            "reason_code": "user_request",
        }
        assert "percent" not in str(events[-1].details)

        with pytest.raises(MemoryAuditError, match="未列入白名单"):
            MemoryService._build_event(
                active.id,
                MemoryEventType.DELETED,
                MemoryActor.USER,
                {"memory_value": {"percent": "30"}},
                clock.current,
            )
    finally:
        await factory.close()


async def test_memory_service_rejects_naive_and_reversed_clock(
    migrated_database_path: Path,
) -> None:
    """无时区或倒退时钟不能生成状态和审计时间，避免跨重启后顺序失真。"""

    factory = SqlAlchemyMemoryUnitOfWorkFactory(DatabaseManager(migrated_database_path))
    await factory.initialize()
    session, message_id = await _create_source_message(factory)

    try:
        naive_service = MemoryService(
            factory,
            clock=MutableClock(datetime(2026, 8, 24, 14, 0)),
        )
        with pytest.raises(MemoryClockError, match="必须返回带时区"):
            await naive_service.create_candidate(_candidate_command(session, message_id))

        clock = MutableClock()
        service = MemoryService(factory, clock=clock)
        candidate = await service.create_candidate(_candidate_command(session, message_id))
        clock.current = NOW - timedelta(seconds=1)
        with pytest.raises(MemoryClockError, match="早于记忆"):
            await service.confirm_candidate(candidate.id)
    finally:
        await factory.close()
