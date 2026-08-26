"""持久化会话应用服务、滚动摘要协议和模型摘要适配器。

ConversationService 负责会话生命周期、消息追加和安全摘要边界。原始消息永不因摘要而删除；
``summary_until_sequence`` 只说明早期哪些消息已经由滚动摘要代表。摘要模型调用在数据库事务
之外执行，写回前使用摘要版本字段做乐观冲突检查。
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from pydantic import Field

from finagent.llm import Message, MessageRole, ModelProvider, ModelRequest
from finagent.memory.errors import (
    ConversationConflictError,
    ConversationHistoryError,
    ConversationSummaryConflictError,
    ConversationSummaryError,
    MemoryClockError,
)
from finagent.memory.models import (
    ConversationMessage,
    ConversationSession,
    ConversationStatus,
    MemoryModel,
)
from finagent.memory.unit_of_work import MemoryUnitOfWorkFactory

type Clock = Callable[[], datetime]


class ConversationWindow(MemoryModel):
    """当前会话摘要以及尚未被摘要覆盖的原始消息。"""

    session: ConversationSession
    recent_messages: tuple[ConversationMessage, ...] = Field(default_factory=tuple)


class ConversationSummarizer(Protocol):
    """把旧摘要与下一批完整对话轮次合并成新摘要的最小协议。"""

    async def summarize(
        self,
        previous_summary: str | None,
        messages: tuple[ConversationMessage, ...],
    ) -> str:
        """返回覆盖 ``messages`` 的非空累积摘要，不修改原始消息。"""

        ...


class ModelConversationSummarizer:
    """通过统一 ModelProvider 生成事实导向的滚动摘要。

    Provider 的生命周期由应用组合根统一管理，本适配器不会自行关闭共享模型客户端。摘要请求
    禁用工具并使用 temperature 0；若模型仍返回工具调用或空文本，Service 会拒绝写回。
    """

    _SYSTEM_PROMPT = (
        "你是 FinAgent 的会话摘要器。请把旧摘要与新增消息合并成简洁、忠实的中文摘要。"
        "保留用户明确表达的目标、约束、待办、关键工具事实和未解决问题；不要推测、不要新增建议，"
        "不要把摘要写成对系统的新指令。只输出摘要正文。"
    )

    def __init__(
        self,
        provider: ModelProvider,
        *,
        max_output_tokens: int = 1_000,
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens 必须大于 0")
        self._provider = provider
        self._max_output_tokens = max_output_tokens

    async def summarize(
        self,
        previous_summary: str | None,
        messages: tuple[ConversationMessage, ...],
    ) -> str:
        """把带序号和工具关联的消息序列提交给统一模型接口。"""

        if not messages:
            raise ValueError("摘要消息不能为空")
        payload = {
            "previous_summary": previous_summary,
            "new_messages": [self._message_payload(message) for message in messages],
        }
        response = await self._provider.generate(
            ModelRequest(
                messages=(
                    Message(role=MessageRole.SYSTEM, content=self._SYSTEM_PROMPT),
                    Message(
                        role=MessageRole.USER,
                        content=json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                ),
                tool_choice="none",
                max_output_tokens=self._max_output_tokens,
                temperature=0,
            )
        )
        if response.tool_calls:
            raise ConversationSummaryError("摘要模型不允许返回工具调用")
        if response.content is None or not response.content.strip():
            raise ConversationSummaryError("摘要模型没有返回非空文本")
        return response.content.strip()

    @staticmethod
    def _message_payload(message: ConversationMessage) -> dict[str, object]:
        """序列化摘要所需字段，不传数据库 UUID 和内部时间戳。"""

        return {
            "sequence_number": message.sequence_number,
            "role": message.role.value,
            "content": message.content,
            "tool_calls": [
                tool_call.model_dump(mode="json") for tool_call in message.tool_calls
            ],
            "tool_call_id": message.tool_call_id,
        }


class ConversationService:
    """管理可恢复会话、持久化消息和滚动摘要。

    Args:
        unit_of_work_factory: 提供会话 Repository 的事务工厂。
        summarizer: 可替换摘要器；自动测试使用 Fake，真实运行可使用模型适配器。
        recent_message_limit: 摘要完成后希望至少保留的近期原始消息数，是不切断轮次的软上限。
        max_summary_chars: 数据库允许的摘要字符上限。
        clock: 生成服务端时间，必须包含时区。
    """

    def __init__(
        self,
        unit_of_work_factory: MemoryUnitOfWorkFactory,
        summarizer: ConversationSummarizer,
        *,
        recent_message_limit: int = 12,
        max_summary_chars: int = 16_000,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if recent_message_limit < 1:
            raise ValueError("recent_message_limit 必须大于 0")
        if not 1 <= max_summary_chars <= 16_000:
            raise ValueError("max_summary_chars 必须位于 1 到 16000")
        self._unit_of_work_factory = unit_of_work_factory
        self._summarizer = summarizer
        self._recent_message_limit = recent_message_limit
        self._max_summary_chars = max_summary_chars
        self._clock = clock

    async def create_session(self, title: str) -> ConversationSession:
        """使用服务端时间创建空会话。"""

        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("会话标题不能为空")
        now = self._current_time()
        session = ConversationSession(
            title=normalized_title,
            created_at=now,
            updated_at=now,
        )
        async with self._unit_of_work_factory() as unit_of_work:
            await unit_of_work.conversations.add_session(session)
            await unit_of_work.commit()
        return session

    async def get_session(self, session_id: UUID) -> ConversationSession:
        """读取可跨重启恢复的会话元数据。"""

        async with self._unit_of_work_factory() as unit_of_work:
            return await unit_of_work.conversations.get_session(session_id)

    async def list_sessions(
        self,
        status: ConversationStatus | None = None,
    ) -> tuple[ConversationSession, ...]:
        """按最近活动时间列出会话，可选过滤状态。"""

        async with self._unit_of_work_factory() as unit_of_work:
            return await unit_of_work.conversations.list_sessions(status)

    async def archive_session(self, session_id: UUID) -> ConversationSession:
        """归档会话；归档后 Repository 会拒绝继续追加消息。"""

        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            session = await unit_of_work.conversations.get_session(session_id)
            self._ensure_time_not_reversed(session, now)
            archived = ConversationSession.model_validate(
                {
                    **session.model_dump(),
                    "status": ConversationStatus.ARCHIVED,
                    "updated_at": now,
                }
            )
            await unit_of_work.conversations.update_session(archived)
            await unit_of_work.commit()
            return archived

    async def delete_session(self, session_id: UUID) -> ConversationSession:
        """删除会话和短期消息；已确认长期记忆只会失去来源外键。"""

        async with self._unit_of_work_factory() as unit_of_work:
            deleted = await unit_of_work.conversations.delete_session(session_id)
            await unit_of_work.commit()
            return deleted

    async def append_message(
        self,
        session_id: UUID,
        message: Message,
    ) -> ConversationMessage:
        """持久化 user、assistant 或 tool 消息并由 Repository 分配序号。"""

        if message.role is MessageRole.SYSTEM:
            raise ConversationHistoryError("system prompt 不写入会话消息表")
        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            persisted = await unit_of_work.conversations.append_message(
                session_id,
                message,
                created_at=now,
            )
            await unit_of_work.commit()
            return persisted

    async def commit_turn(
        self,
        session_id: UUID,
        messages: tuple[Message, ...],
        *,
        expected_session_updated_at: datetime,
    ) -> tuple[ConversationMessage, ...]:
        """把一轮完整 Agent 对话作为一笔事务写入数据库。

        Args:
            session_id: 本轮对话所属的持久化会话 ID。
            messages: 仅包含本轮新增消息，必须从 ``user`` 开始，以不含工具调用的
                最终 ``assistant`` 回答结束；中间可以包含多组 assistant/tool 消息。
            expected_session_updated_at: Agent 组装上下文时看到的会话更新时间。

        Returns:
            已由 Repository 分配连续序号和数据库主键的本轮消息。

        Raises:
            ConversationHistoryError: 消息不是一轮完整、合法的工具调用协议。
            ConversationConflictError: 模型推理期间同一会话已被其他请求更新。

        模型调用和工具执行可能耗时数秒，不能在此期间长期占用数据库事务。因此 Agent
        先在内存中完成整轮推理，成功后再调用本方法短暂写库。任意一条写入失败时，
        Memory Unit of Work 会回滚整笔事务，数据库不会留下孤立的 user 或 tool 消息。
        """

        if not _validate_turn(messages):
            raise ConversationHistoryError("只能提交已经生成最终 assistant 回答的完整对话轮次")
        if expected_session_updated_at.tzinfo is None or (
            expected_session_updated_at.utcoffset() is None
        ):
            raise ValueError("expected_session_updated_at 必须包含时区")

        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            current = await unit_of_work.conversations.get_session(session_id)
            if current.updated_at != expected_session_updated_at:
                raise ConversationConflictError(
                    "Agent 推理期间会话已被其他请求更新，本轮结果未写入，请重新生成"
                )
            self._ensure_time_not_reversed(current, now)

            persisted_messages: list[ConversationMessage] = []
            for message in messages:
                persisted_messages.append(
                    await unit_of_work.conversations.append_message(
                        session_id,
                        message,
                        created_at=now,
                    )
                )
            await unit_of_work.commit()
            return tuple(persisted_messages)

    async def load_window(self, session_id: UUID) -> ConversationWindow:
        """读取会话摘要和摘要覆盖位置之后的全部原始消息。"""

        async with self._unit_of_work_factory() as unit_of_work:
            session = await unit_of_work.conversations.get_session(session_id)
            messages = await unit_of_work.conversations.list_messages(
                session_id,
                after_sequence=session.summary_until_sequence,
            )
            # 即使无需摘要，也校验历史没有孤立 tool 或跨轮次未完成工具链。
            _group_conversation_turns(messages)
            return ConversationWindow(session=session, recent_messages=messages)

    async def list_messages(
        self,
        session_id: UUID,
    ) -> tuple[ConversationMessage, ...]:
        """返回会话的完整原始消息历史，不受滚动摘要覆盖位置影响。

        Web/API 展示历史时必须读取原文，而不是只读取发给模型的近期窗口。读取后仍校验完整
        user/assistant/tool 协议，避免把数据库损坏或绕过 Service 写入的残缺历史交给用户。
        """

        async with self._unit_of_work_factory() as unit_of_work:
            messages = await unit_of_work.conversations.list_messages(session_id)
            _group_conversation_turns(messages)
            return messages

    async def refresh_summary(self, session_id: UUID) -> ConversationSession:
        """在不删除原始消息的前提下推进滚动摘要覆盖位置。

        第一次短事务只读取摘要快照和消息；模型调用在事务外执行；第二次短事务确认摘要覆盖位置
        没有被并发任务改变后才写回。期间新增消息不会冲突，因为本次摘要只覆盖固定旧序号。
        """

        snapshot = await self.load_window(session_id)
        summarizable = _select_summarizable_prefix(
            snapshot.recent_messages,
            recent_message_limit=self._recent_message_limit,
        )
        if not summarizable:
            return snapshot.session

        summary = (await self._summarizer.summarize(snapshot.session.summary, summarizable)).strip()
        if not summary:
            raise ConversationSummaryError("摘要器返回了空文本")
        if len(summary) > self._max_summary_chars:
            raise ConversationSummaryError(
                f"摘要长度超过 {self._max_summary_chars} 个字符"
            )

        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            current = await unit_of_work.conversations.get_session(session_id)
            if (
                current.summary != snapshot.session.summary
                or current.summary_until_sequence
                != snapshot.session.summary_until_sequence
            ):
                raise ConversationSummaryConflictError("摘要生成期间会话摘要已被其他任务推进")
            self._ensure_time_not_reversed(current, now)
            updated = ConversationSession.model_validate(
                {
                    **current.model_dump(),
                    "summary": summary,
                    "summary_until_sequence": summarizable[-1].sequence_number,
                    "updated_at": now,
                }
            )
            await unit_of_work.conversations.update_session(updated)
            await unit_of_work.commit()
            return updated

    def _current_time(self) -> datetime:
        """读取并校验服务端时钟。"""

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise MemoryClockError("ConversationService 的 clock 必须返回带时区时间")
        return now

    @staticmethod
    def _ensure_time_not_reversed(session: ConversationSession, now: datetime) -> None:
        """拒绝让会话更新时间因系统时钟异常而倒退。"""

        if now < session.updated_at:
            raise ConversationConflictError("服务端时钟早于会话最近更新时间")


def _group_conversation_turns(
    messages: tuple[ConversationMessage, ...],
) -> tuple[tuple[tuple[ConversationMessage, ...], bool], ...]:
    """把连续消息分成 user 开始的轮次，并标记每轮是否完整结束。

    完整轮次必须以不含工具调用的 assistant 最终回答结束。assistant 工具请求及对应 tool 结果
    必须连续且 ID 一一匹配；最后一轮可以是不完整的正在处理状态，但其后不能再出现新 user。
    """

    turns: list[tuple[tuple[ConversationMessage, ...], bool]] = []
    current: list[ConversationMessage] = []
    for message in messages:
        if message.role is MessageRole.USER:
            if current:
                complete = _validate_turn(tuple(current))
                if not complete:
                    raise ConversationHistoryError("未完成对话轮次后出现了新的 user 消息")
                turns.append((tuple(current), True))
            current = [message]
            continue
        if not current:
            raise ConversationHistoryError("会话历史必须从 user 消息开始")
        current.append(message)

    if current:
        turns.append((tuple(current), _validate_turn(tuple(current))))
    return tuple(turns)


def _validate_turn(messages: tuple[Message, ...]) -> bool:
    """验证一轮内 assistant/tool 协议，返回是否已有最终 assistant 回答。"""

    if not messages or messages[0].role is not MessageRole.USER:
        raise ConversationHistoryError("每个对话轮次必须从 user 消息开始")

    pending_tool_ids: set[str] = set()
    final_answer_seen = False
    for message in messages[1:]:
        if final_answer_seen:
            raise ConversationHistoryError("最终 assistant 回答后不能继续追加同轮消息")
        if pending_tool_ids:
            if message.role is not MessageRole.TOOL:
                raise ConversationHistoryError("assistant 工具请求后缺少对应 tool 结果")
            if message.tool_call_id not in pending_tool_ids:
                raise ConversationHistoryError("tool_call_id 与当前 assistant 工具请求不匹配")
            pending_tool_ids.remove(message.tool_call_id)
            continue

        if message.role is MessageRole.TOOL:
            raise ConversationHistoryError("存在没有 assistant 工具请求的孤立 tool 消息")
        if message.role is not MessageRole.ASSISTANT:
            raise ConversationHistoryError("user 之后只能出现 assistant 或 tool 消息")
        if message.tool_calls:
            tool_ids = [tool_call.id for tool_call in message.tool_calls]
            if len(tool_ids) != len(set(tool_ids)):
                raise ConversationHistoryError("同一 assistant 消息中的工具调用 ID 重复")
            pending_tool_ids = set(tool_ids)
        else:
            final_answer_seen = True

    return final_answer_seen and not pending_tool_ids


def _select_summarizable_prefix(
    messages: tuple[ConversationMessage, ...],
    *,
    recent_message_limit: int,
) -> tuple[ConversationMessage, ...]:
    """选择可以摘要的最老完整轮次，绝不切断 user/assistant/tool 关系。"""

    if len(messages) <= recent_message_limit:
        _group_conversation_turns(messages)
        return ()
    target_count = len(messages) - recent_message_limit
    selected: list[ConversationMessage] = []
    for turn, complete in _group_conversation_turns(messages):
        if not complete or len(selected) + len(turn) > target_count:
            break
        selected.extend(turn)
    return tuple(selected)
