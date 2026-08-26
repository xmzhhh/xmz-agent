"""持久化 Agent 会话与结构化记忆管理的 FastAPI 路由。

路由只负责 HTTP 传输和调用应用服务。聊天模型只能生成 ``candidate``；确认、拒绝和删除
必须通过独立端点显式触发，避免自然语言对话被误当成高风险状态修改授权。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from finagent.agents import AgentApplicationService, AgentChatResult
from finagent.memory import (
    ConversationMessage,
    ConversationService,
    ConversationSession,
    ConversationStatus,
    MemoryConfirmationResult,
    MemoryEvent,
    MemoryItem,
    MemoryRejectionReason,
    MemoryScopeType,
    MemoryService,
    MemoryStatus,
    MemoryType,
)


class AgentUnavailableError(RuntimeError):
    """当前进程没有模型配置，无法执行 Agent 聊天。"""


class ConversationCreateRequest(BaseModel):
    """创建会话所需的用户可编辑字段。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        """去除首尾空白，并在 HTTP 边界拒绝空标题。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("title 不能为空")
        return normalized


class AgentChatRequest(BaseModel):
    """一轮聊天输入及用于筛选资产范围记忆的显式代码。"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    asset_symbols: tuple[str, ...] = Field(default_factory=tuple, max_length=20)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        """去除首尾空白，并拒绝只包含空白的消息。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("message 不能为空")
        return normalized

    @field_validator("asset_symbols")
    @classmethod
    def normalize_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """统一资产代码格式并拒绝空值或重复值。"""

        normalized = tuple(symbol.strip().upper() for symbol in value)
        if any(not symbol for symbol in normalized):
            raise ValueError("asset_symbols 不能包含空代码")
        if len(normalized) != len(set(normalized)):
            raise ValueError("asset_symbols 不能重复")
        return normalized


class MemoryRejectRequest(BaseModel):
    """拒绝候选时允许写入审计日志的枚举原因。"""

    model_config = ConfigDict(extra="forbid")

    reason: MemoryRejectionReason = MemoryRejectionReason.USER_DECISION


def create_agent_router(
    conversations: ConversationService,
    memories: MemoryService,
    agent: AgentApplicationService | None,
    *,
    unavailable_reason: str | None = None,
) -> APIRouter:
    """创建共享同一持久化服务实例的 Agent 与记忆 API 路由。"""

    router = APIRouter(prefix="/api/v1")

    def require_agent() -> AgentApplicationService:
        """只在聊天端点要求模型，离线会话和记忆管理仍可使用。"""

        if agent is None:
            raise AgentUnavailableError(
                unavailable_reason or "当前应用未装配 Agent 模型服务"
            )
        return agent

    @router.post(
        "/agent/sessions",
        response_model=ConversationSession,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(data: ConversationCreateRequest) -> ConversationSession:
        """创建可跨应用重启恢复的会话。"""

        return await conversations.create_session(data.title)

    @router.get("/agent/sessions", response_model=list[ConversationSession])
    async def list_sessions(
        session_status: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    ) -> tuple[ConversationSession, ...]:
        """列出全部会话，可用 ``status`` 查询参数过滤。"""

        return await conversations.list_sessions(session_status)

    @router.get("/agent/sessions/{session_id}", response_model=ConversationSession)
    async def get_session(session_id: UUID) -> ConversationSession:
        """读取单个会话元数据。"""

        return await conversations.get_session(session_id)

    @router.delete("/agent/sessions/{session_id}", response_model=ConversationSession)
    async def delete_session(session_id: UUID) -> ConversationSession:
        """删除会话及其短期消息，不自动删除已确认长期记忆。"""

        return await conversations.delete_session(session_id)

    @router.get(
        "/agent/sessions/{session_id}/messages",
        response_model=list[ConversationMessage],
    )
    async def list_messages(session_id: UUID) -> tuple[ConversationMessage, ...]:
        """按顺序读取完整会话历史，包含工具调用和工具结果。"""

        return await conversations.list_messages(session_id)

    @router.post(
        "/agent/sessions/{session_id}/chat",
        response_model=AgentChatResult,
    )
    async def chat(session_id: UUID, data: AgentChatRequest) -> AgentChatResult:
        """运行一轮只读 Agent，并在回答落库后尽力产生候选记忆。"""

        return await require_agent().chat(
            session_id,
            data.message,
            asset_symbols=data.asset_symbols,
        )

    @router.get("/memories/candidates", response_model=list[MemoryItem])
    async def list_candidates() -> tuple[MemoryItem, ...]:
        """返回等待用户确认或拒绝的候选记忆。"""

        return await memories.list_memories(status=MemoryStatus.CANDIDATE)

    @router.get("/memories", response_model=list[MemoryItem])
    async def list_memories(
        memory_status: Annotated[MemoryStatus | None, Query(alias="status")] = None,
        memory_type: MemoryType | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
    ) -> tuple[MemoryItem, ...]:
        """按状态、类型和作用域组合查询长期记忆。"""

        return await memories.list_memories(
            status=memory_status,
            memory_type=memory_type,
            scope_type=scope_type,
            scope_id=scope_id,
        )

    @router.get("/memories/{memory_id}", response_model=MemoryItem)
    async def get_memory(memory_id: UUID) -> MemoryItem:
        """读取一条候选、活动或历史记忆正文。"""

        return await memories.get_memory(memory_id)

    @router.get("/memories/{memory_id}/events", response_model=list[MemoryEvent])
    async def list_memory_events(memory_id: UUID) -> tuple[MemoryEvent, ...]:
        """读取不含记忆正文的审计轨迹。"""

        return await memories.list_events(memory_id)

    @router.post(
        "/memories/{memory_id}/confirm",
        response_model=MemoryConfirmationResult,
    )
    async def confirm_memory(memory_id: UUID) -> MemoryConfirmationResult:
        """执行独立的用户确认动作，使候选成为 ACTIVE。"""

        return await memories.confirm_candidate(memory_id)

    @router.post("/memories/{memory_id}/reject", response_model=MemoryItem)
    async def reject_memory(
        memory_id: UUID,
        data: MemoryRejectRequest,
    ) -> MemoryItem:
        """拒绝候选并记录有限枚举原因，不保存自由文本。"""

        return await memories.reject_candidate(memory_id, reason=data.reason)

    @router.delete("/memories/{memory_id}", response_model=MemoryItem)
    async def delete_memory(memory_id: UUID) -> MemoryItem:
        """按用户要求硬删除记忆正文，并保留最小删除审计。"""

        return await memories.delete_memory(memory_id)

    return router
