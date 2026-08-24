"""会话消息与结构化记忆的 SQLAlchemy Repository 实现。

两个 Repository 都绑定外部传入的 :class:`AsyncSession`，只执行查询、写入、``flush``
和领域模型转换，不自行 ``commit``。同一个 Memory Unit of Work 因而可以把消息、记忆正文
和审计事件作为一笔事务提交或回滚。
"""

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from finagent.llm import Message, MessageRole, ToolCall
from finagent.memory.errors import (
    ConversationArchivedError,
    ConversationConflictError,
    ConversationMessageNotFoundError,
    ConversationNotFoundError,
    MemoryConflictError,
    MemoryItemNotFoundError,
)
from finagent.memory.models import (
    ConversationMessage,
    ConversationSession,
    ConversationStatus,
    MemoryActor,
    MemoryEvent,
    MemoryEventType,
    MemoryItem,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
)
from finagent.persistence.errors import PersistenceError
from finagent.persistence.memory_models import (
    ChatMessageRow,
    ChatSessionRow,
    MemoryEventRow,
    MemoryItemRow,
)


def _dump_json(value: Any) -> str:
    """生成稳定、紧凑且保留中文的 JSON，便于测试、审计与跨版本比较。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_json(raw_value: str, *, field_name: str) -> Any:
    """解析数据库 JSON；损坏数据应明确失败，不能静默替换为空结构。"""

    try:
        return json.loads(raw_value)
    except (json.JSONDecodeError, TypeError) as error:
        raise PersistenceError(f"数据库字段 {field_name} 不是合法 JSON") from error


class SqlAlchemyConversationRepository:
    """在当前 AsyncSession 中持久化会话及其有序消息。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_session(self, session: ConversationSession) -> ConversationSession:
        """创建会话，并在 flush 阶段把主键冲突转换为领域异常。"""

        row = ChatSessionRow(
            id=session.id,
            title=session.title,
            status=session.status.value,
            summary=session.summary,
            summary_until_sequence=session.summary_until_sequence,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )
        self._session.add(row)
        try:
            # 先 flush 父会话，后续同一事务才能安全追加带外键的消息。
            await self._session.flush()
        except IntegrityError as error:
            raise ConversationConflictError(f"会话已存在：{session.id}") from error
        return self._session_row_to_domain(row)

    async def get_session(self, session_id: UUID) -> ConversationSession:
        """按 UUID 读取会话。"""

        return self._session_row_to_domain(await self._require_session_row(session_id))

    async def list_sessions(
        self,
        status: ConversationStatus | None = None,
    ) -> tuple[ConversationSession, ...]:
        """最近更新的会话优先；UUID 用于解决相同时间下的稳定排序。"""

        statement = select(ChatSessionRow)
        if status is not None:
            statement = statement.where(ChatSessionRow.status == status.value)
        statement = statement.order_by(ChatSessionRow.updated_at.desc(), ChatSessionRow.id)
        rows = (await self._session.scalars(statement)).all()
        return tuple(self._session_row_to_domain(row) for row in rows)

    async def update_session(self, session: ConversationSession) -> ConversationSession:
        """更新会话可变字段，同时保护创建时间不被回写。"""

        row = await self._require_session_row(session.id)
        if row.created_at != session.created_at:
            raise ConversationConflictError("会话创建时间是不可变字段")

        row.title = session.title
        row.status = session.status.value
        row.summary = session.summary
        row.summary_until_sequence = session.summary_until_sequence
        row.updated_at = session.updated_at
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise ConversationConflictError(f"会话更新冲突：{session.id}") from error
        return self._session_row_to_domain(row)

    async def delete_session(self, session_id: UUID) -> ConversationSession:
        """删除会话；SQLite 外键会级联删除其短期消息。"""

        row = await self._require_session_row(session_id)
        session = self._session_row_to_domain(row)
        await self._session.delete(row)
        await self._session.flush()
        return session

    async def append_message(
        self,
        session_id: UUID,
        message: Message,
        *,
        created_at: datetime,
    ) -> ConversationMessage:
        """分配连续序号并追加消息，同时刷新会话的最近活动时间。"""

        session_row = await self._require_session_row(session_id)
        if ConversationStatus(session_row.status) is ConversationStatus.ARCHIVED:
            raise ConversationArchivedError(f"会话已归档，不能追加消息：{session_id}")
        if created_at < session_row.updated_at:
            raise ConversationConflictError("新消息时间不能早于会话最近更新时间")

        latest_sequence = cast(
            int | None,
            await self._session.scalar(
                select(func.max(ChatMessageRow.sequence_number)).where(
                    ChatMessageRow.session_id == session_id
                )
            ),
        )
        persisted = ConversationMessage(
            session_id=session_id,
            sequence_number=(latest_sequence or 0) + 1,
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
            created_at=created_at,
        )
        row = ChatMessageRow(
            id=persisted.id,
            session_id=persisted.session_id,
            sequence_number=persisted.sequence_number,
            role=persisted.role.value,
            content=persisted.content,
            tool_calls_json=_dump_json(
                [tool_call.model_dump(mode="json") for tool_call in persisted.tool_calls]
            ),
            tool_call_id=persisted.tool_call_id,
            created_at=persisted.created_at,
        )
        self._session.add(row)
        session_row.updated_at = created_at
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise ConversationConflictError(
                f"会话消息序号写入冲突：{session_id}#{persisted.sequence_number}"
            ) from error
        return self._message_row_to_domain(row)

    async def list_messages(
        self,
        session_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[ConversationMessage, ...]:
        """读取一个会话的消息窗口；即使消息为空，也先确认会话确实存在。"""

        if after_sequence < 0:
            raise ValueError("after_sequence 不能小于 0")
        if limit is not None and limit < 1:
            raise ValueError("limit 必须大于 0")
        await self._require_session_row(session_id)

        statement = (
            select(ChatMessageRow)
            .where(
                ChatMessageRow.session_id == session_id,
                ChatMessageRow.sequence_number > after_sequence,
            )
            .order_by(ChatMessageRow.sequence_number)
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = (await self._session.scalars(statement)).all()
        return tuple(self._message_row_to_domain(row) for row in rows)

    async def get_message(self, message_id: UUID) -> ConversationMessage:
        """按消息 UUID 读取来源消息，不要求调用方先知道其会话。"""

        row = await self._session.get(ChatMessageRow, message_id)
        if row is None:
            raise ConversationMessageNotFoundError(f"会话消息不存在：{message_id}")
        return self._message_row_to_domain(row)

    async def _require_session_row(self, session_id: UUID) -> ChatSessionRow:
        """读取会话 ORM Row，并统一不存在时的异常类型。"""

        row = await self._session.get(ChatSessionRow, session_id)
        if row is None:
            raise ConversationNotFoundError(f"会话不存在：{session_id}")
        return row

    @staticmethod
    def _session_row_to_domain(row: ChatSessionRow) -> ConversationSession:
        """把会话 ORM Row 转换为严格、不可变的领域模型。"""

        return ConversationSession(
            id=row.id,
            title=row.title,
            status=ConversationStatus(row.status),
            summary=row.summary,
            summary_until_sequence=row.summary_until_sequence,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _message_row_to_domain(row: ChatMessageRow) -> ConversationMessage:
        """恢复消息和工具调用 JSON，并再次执行角色字段校验。"""

        tool_calls_value = _load_json(row.tool_calls_json, field_name="tool_calls_json")
        if not isinstance(tool_calls_value, list):
            raise PersistenceError("数据库字段 tool_calls_json 必须是 JSON 数组")
        tool_calls = tuple(ToolCall.model_validate(item) for item in tool_calls_value)
        return ConversationMessage(
            id=row.id,
            session_id=row.session_id,
            sequence_number=row.sequence_number,
            role=MessageRole(row.role),
            content=row.content,
            tool_calls=tool_calls,
            tool_call_id=row.tool_call_id,
            created_at=row.created_at,
        )


class SqlAlchemyMemoryRepository:
    """在当前 AsyncSession 中持久化长期记忆正文和独立审计事件。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_memory(self, memory: MemoryItem) -> MemoryItem:
        """新增记忆；唯一 ACTIVE 身份冲突会转换为可理解的领域异常。"""

        row = MemoryItemRow(
            id=memory.id,
            memory_type=memory.memory_type.value,
            memory_key=memory.memory_key,
            value_json=_dump_json(memory.value),
            scope_type=memory.scope_type.value,
            scope_id=memory.scope_id or "",
            status=memory.status.value,
            source_session_id=memory.source_session_id,
            source_message_id=memory.source_message_id,
            supersedes_id=memory.supersedes_id,
            version=memory.version,
            expires_at=memory.expires_at,
            confirmed_at=memory.confirmed_at,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise MemoryConflictError(f"长期记忆写入冲突：{memory.id}") from error
        return self._memory_row_to_domain(row)

    async def get_memory(self, memory_id: UUID) -> MemoryItem:
        """按 UUID 读取长期记忆。"""

        return self._memory_row_to_domain(await self._require_memory_row(memory_id))

    async def list_memories(
        self,
        *,
        status: MemoryStatus | None = None,
        memory_type: MemoryType | None = None,
        memory_key: str | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
    ) -> tuple[MemoryItem, ...]:
        """组合状态、类型和范围条件，返回确定顺序的记忆集合。"""

        if scope_id is not None and scope_type is None:
            raise ValueError("查询 scope_id 时必须同时提供 scope_type")
        normalized_scope_id = scope_id.strip().upper() if scope_id is not None else None
        if scope_type is MemoryScopeType.GLOBAL and normalized_scope_id is not None:
            raise ValueError("查询全局记忆时不能提供 scope_id")
        if scope_type is MemoryScopeType.ASSET and not normalized_scope_id:
            raise ValueError("查询资产记忆时必须提供 scope_id")

        statement = select(MemoryItemRow)
        if status is not None:
            statement = statement.where(MemoryItemRow.status == status.value)
        if memory_type is not None:
            statement = statement.where(MemoryItemRow.memory_type == memory_type.value)
        if memory_key is not None:
            statement = statement.where(MemoryItemRow.memory_key == memory_key)
        if scope_type is not None:
            statement = statement.where(MemoryItemRow.scope_type == scope_type.value)
            statement = statement.where(MemoryItemRow.scope_id == (normalized_scope_id or ""))
        statement = statement.order_by(MemoryItemRow.created_at, MemoryItemRow.id)
        rows = (await self._session.scalars(statement)).all()
        return tuple(self._memory_row_to_domain(row) for row in rows)

    async def update_memory(self, memory: MemoryItem) -> MemoryItem:
        """更新记忆字段；确认或替换的业务合法性由后续 MemoryService 决定。"""

        row = await self._require_memory_row(memory.id)
        if row.created_at != memory.created_at:
            raise MemoryConflictError("长期记忆创建时间是不可变字段")

        row.memory_type = memory.memory_type.value
        row.memory_key = memory.memory_key
        row.value_json = _dump_json(memory.value)
        row.scope_type = memory.scope_type.value
        row.scope_id = memory.scope_id or ""
        row.status = memory.status.value
        row.source_session_id = memory.source_session_id
        row.source_message_id = memory.source_message_id
        row.supersedes_id = memory.supersedes_id
        row.version = memory.version
        row.expires_at = memory.expires_at
        row.confirmed_at = memory.confirmed_at
        row.updated_at = memory.updated_at
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise MemoryConflictError(f"长期记忆更新冲突：{memory.id}") from error
        return self._memory_row_to_domain(row)

    async def delete_memory(self, memory_id: UUID) -> MemoryItem:
        """硬删除记忆正文，审计事件因没有外键而继续保留。"""

        row = await self._require_memory_row(memory_id)
        memory = self._memory_row_to_domain(row)
        await self._session.delete(row)
        await self._session.flush()
        return memory

    async def add_event(self, event: MemoryEvent) -> MemoryEvent:
        """追加审计事件；details 只负责稳定 JSON 存储，白名单由 Service 校验。"""

        row = MemoryEventRow(
            id=event.id,
            memory_id=event.memory_id,
            event_type=event.event_type.value,
            actor=event.actor.value,
            details_json=_dump_json(event.details),
            occurred_at=event.occurred_at,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise MemoryConflictError(f"记忆事件写入冲突：{event.id}") from error
        return self._event_row_to_domain(row)

    async def list_events(self, memory_id: UUID) -> tuple[MemoryEvent, ...]:
        """按发生时间和 UUID 返回确定顺序的审计轨迹。"""

        rows = (
            await self._session.scalars(
                select(MemoryEventRow)
                .where(MemoryEventRow.memory_id == memory_id)
                .order_by(MemoryEventRow.occurred_at, MemoryEventRow.id)
            )
        ).all()
        return tuple(self._event_row_to_domain(row) for row in rows)

    async def _require_memory_row(self, memory_id: UUID) -> MemoryItemRow:
        """读取记忆 ORM Row，并统一不存在时的异常类型。"""

        row = await self._session.get(MemoryItemRow, memory_id)
        if row is None:
            raise MemoryItemNotFoundError(f"长期记忆不存在：{memory_id}")
        return row

    @staticmethod
    def _memory_row_to_domain(row: MemoryItemRow) -> MemoryItem:
        """恢复记忆 JSON，并隐藏全局范围使用空字符串的数据库细节。"""

        value = _load_json(row.value_json, field_name="value_json")
        if not isinstance(value, dict):
            raise PersistenceError("数据库字段 value_json 必须是 JSON 对象")
        return MemoryItem(
            id=row.id,
            memory_type=MemoryType(row.memory_type),
            memory_key=row.memory_key,
            value=cast(dict[str, Any], value),
            scope_type=MemoryScopeType(row.scope_type),
            scope_id=row.scope_id or None,
            status=MemoryStatus(row.status),
            source_session_id=row.source_session_id,
            source_message_id=row.source_message_id,
            supersedes_id=row.supersedes_id,
            version=row.version,
            expires_at=row.expires_at,
            confirmed_at=row.confirmed_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _event_row_to_domain(row: MemoryEventRow) -> MemoryEvent:
        """恢复不含正文的审计事件。"""

        details = _load_json(row.details_json, field_name="details_json")
        if not isinstance(details, dict):
            raise PersistenceError("数据库字段 details_json 必须是 JSON 对象")
        return MemoryEvent(
            id=row.id,
            memory_id=row.memory_id,
            event_type=MemoryEventType(row.event_type),
            actor=MemoryActor(row.actor),
            details=cast(dict[str, Any], details),
            occurred_at=row.occurred_at,
        )
