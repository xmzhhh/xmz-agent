"""面向 Web/CLI 的持久化 Agent 应用服务。

本模块把一轮 Agent 对话和长期记忆候选生成连接起来，但刻意不把两者放进同一数据库事务：
对话已经成功回答并完整落库后，候选抽取失败只应产生可见警告，不能把已经完成的对话伪装成
失败。候选仍必须经过独立的用户确认接口，模型没有任何直接激活长期记忆的路径。
"""

from collections.abc import Iterable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from finagent.agents.persistent import PersistentAgentTurnResult, PersistentToolCallingAgent
from finagent.llm import ModelProviderError
from finagent.memory import (
    ConversationMessage,
    ConversationService,
    ConversationSession,
    ConversationStatus,
    MemoryCandidateCreate,
    MemoryCandidateExtractor,
    MemoryExtractionError,
    MemoryItem,
    MemoryService,
    SensitiveMemoryError,
)


class AgentChatResult(BaseModel):
    """一次 Web/CLI 对话返回的最终回答和可选记忆候选。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: PersistentAgentTurnResult
    memory_candidate: MemoryItem | None = None
    memory_warning: str | None = None


class AgentApplicationService:
    """统一编排会话生命周期、Agent 对话和候选记忆生成。"""

    def __init__(
        self,
        agent: PersistentToolCallingAgent,
        conversations: ConversationService,
        memories: MemoryService,
        candidate_extractor: MemoryCandidateExtractor,
    ) -> None:
        self._agent = agent
        self._conversations = conversations
        self._memories = memories
        self._candidate_extractor = candidate_extractor

    async def create_session(self, title: str) -> ConversationSession:
        """创建一条可跨进程恢复的新会话。"""

        return await self._conversations.create_session(title)

    async def get_session(self, session_id: UUID) -> ConversationSession:
        """读取会话元数据。"""

        return await self._conversations.get_session(session_id)

    async def list_sessions(
        self,
        status: ConversationStatus | None = None,
    ) -> tuple[ConversationSession, ...]:
        """按更新时间列出会话，可选过滤状态。"""

        return await self._conversations.list_sessions(status)

    async def list_messages(
        self,
        session_id: UUID,
    ) -> tuple[ConversationMessage, ...]:
        """返回会话完整原始消息，供页面恢复工具调用轨迹。"""

        return await self._conversations.list_messages(session_id)

    async def delete_session(self, session_id: UUID) -> ConversationSession:
        """删除会话与短期消息；长期记忆按既有外键规则保留。"""

        return await self._conversations.delete_session(session_id)

    async def chat(
        self,
        session_id: UUID,
        user_input: str,
        *,
        asset_symbols: Iterable[str] = (),
    ) -> AgentChatResult:
        """完成并持久化一轮 Agent 对话，再尽力生成一条待确认记忆。

        主 Agent 失败会原样抛出，因为此时整轮消息没有写入；候选抽取或敏感信息拦截发生
        在最终回答落库之后，因此降级成 ``memory_warning``，回答仍可正常展示。
        """

        turn = await self._agent.ask(
            session_id,
            user_input,
            asset_symbols=asset_symbols,
        )
        source_message = turn.messages[0]
        try:
            draft = await self._candidate_extractor.extract(source_message, turn.answer)
        except (MemoryExtractionError, ModelProviderError):
            return AgentChatResult(
                turn=turn,
                memory_warning="回答已保存，但本轮候选记忆抽取失败",
            )

        if draft is None:
            return AgentChatResult(turn=turn)

        try:
            candidate = await self._memories.create_candidate(
                MemoryCandidateCreate(
                    **draft.model_dump(),
                    source_session_id=source_message.session_id,
                    source_message_id=source_message.id,
                )
            )
        except SensitiveMemoryError:
            return AgentChatResult(
                turn=turn,
                memory_warning="回答已保存，但候选内容涉及敏感信息，未写入长期记忆",
            )
        return AgentChatResult(turn=turn, memory_candidate=candidate)

    async def close(self) -> None:
        """关闭 Agent 独占生命周期的共享模型 Provider。"""

        await self._agent.close()
