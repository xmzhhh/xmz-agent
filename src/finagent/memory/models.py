"""短期会话记忆与长期结构化记忆的领域模型。

短期记忆按会话隔离，用于恢复最近消息、工具调用链和滚动摘要；长期记忆跨会话生效，但只
保存用户确认的偏好、约束、关注项、目标和反馈。当前持仓、行情与收益仍由资产数据库和只读
工具提供，不复制成容易过期的长期记忆。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from finagent.llm import Message


class MemoryModel(BaseModel):
    """记忆领域模型共享的严格、不可变配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationStatus(StrEnum):
    """会话当前是否仍允许继续追加消息。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class MemoryType(StrEnum):
    """长期记忆允许保存的稳定信息类型。"""

    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    WATCHLIST = "watchlist"
    GOAL = "goal"
    FEEDBACK = "feedback"


class MemoryScopeType(StrEnum):
    """记忆的适用范围；资产范围必须同时提供资产代码。"""

    GLOBAL = "global"
    ASSET = "asset"


class MemoryStatus(StrEnum):
    """候选记忆从产生到退出上下文的完整状态。"""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MemoryEventType(StrEnum):
    """记忆审计记录允许出现的操作。"""

    CANDIDATE_CREATED = "candidate_created"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DELETED = "deleted"


class MemoryActor(StrEnum):
    """触发记忆事件的主体，用于区分模型建议和用户决定。"""

    USER = "user"
    MODEL = "model"
    SYSTEM = "system"


def _require_timezone(value: datetime) -> datetime:
    """拒绝无时区时间，保证跨进程恢复后仍能正确比较先后顺序。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("记忆时间必须包含时区")
    return value


def _normalize_optional_text(value: Any) -> Any:
    """去除可选文本首尾空白，并把空字符串统一视为未填写。"""

    if not isinstance(value, str):
        return value
    normalized = value.strip()
    return normalized or None


class ConversationSession(MemoryModel):
    """一个可跨应用重启恢复的独立对话会话。

    ``summary`` 只压缩本会话较早的消息，不能自动升级为跨会话长期记忆；
    ``summary_until_sequence`` 明确摘要覆盖到哪条消息，避免摘要和近期窗口重复注入。
    """

    id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=120)
    status: ConversationStatus = ConversationStatus.ACTIVE
    summary: str | None = Field(default=None, min_length=1, max_length=16_000)
    summary_until_sequence: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    _normalize_summary = field_validator("summary", mode="before")(_normalize_optional_text)
    _validate_created_at = field_validator("created_at")(_require_timezone)
    _validate_updated_at = field_validator("updated_at")(_require_timezone)

    @model_validator(mode="after")
    def validate_summary_and_time_order(self) -> Self:
        """保证摘要覆盖位置与摘要内容一致，并拒绝倒退的更新时间。"""

        if self.updated_at < self.created_at:
            raise ValueError("会话更新时间不能早于创建时间")
        if self.summary is None and self.summary_until_sequence != 0:
            raise ValueError("没有会话摘要时 summary_until_sequence 必须为 0")
        if self.summary is not None and self.summary_until_sequence == 0:
            raise ValueError("存在会话摘要时必须记录其覆盖的消息序号")
        return self


class ConversationMessage(Message):
    """写入短期记忆的一条标准消息。

    继承模型层 ``Message`` 的角色约束，因而 assistant 工具请求与 tool 结果仍必须携带正确
    的 ``tool_calls`` 或 ``tool_call_id``。新增字段只负责数据库身份、顺序和时间。
    """

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    sequence_number: int = Field(ge=1)
    created_at: datetime

    _validate_created_at = field_validator("created_at")(_require_timezone)


class MemoryItem(MemoryModel):
    """一条候选或已确认的长期结构化记忆。

    ``value`` 必须是 JSON 对象，便于按字段校验和演进；记忆原文通过来源消息追溯，不在
    value 中复制整段聊天。ACTIVE、SUPERSEDED、EXPIRED 都表示用户曾确认过该记忆，必须
    保留 ``confirmed_at``；CANDIDATE 和 REJECTED 则不能伪装成已确认内容。
    """

    id: UUID = Field(default_factory=uuid4)
    memory_type: MemoryType
    memory_key: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]*$",
    )
    value: dict[str, JsonValue] = Field(min_length=1)
    scope_type: MemoryScopeType = MemoryScopeType.GLOBAL
    scope_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Z0-9._-]+$",
    )
    status: MemoryStatus = MemoryStatus.CANDIDATE
    source_session_id: UUID | None = None
    source_message_id: UUID | None = None
    supersedes_id: UUID | None = None
    version: int = Field(default=1, ge=1)
    expires_at: datetime | None = None
    confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    _normalize_scope_id = field_validator("scope_id", mode="before")(
        lambda value: value.strip().upper() if isinstance(value, str) else value
    )
    _validate_expires_at = field_validator("expires_at")(
        lambda value: _require_timezone(value) if value is not None else value
    )
    _validate_confirmed_at = field_validator("confirmed_at")(
        lambda value: _require_timezone(value) if value is not None else value
    )
    _validate_created_at = field_validator("created_at")(_require_timezone)
    _validate_updated_at = field_validator("updated_at")(_require_timezone)

    @model_validator(mode="after")
    def validate_memory_state(self) -> Self:
        """校验范围、来源、确认状态、版本链和时间顺序之间的关系。"""

        if self.scope_type is MemoryScopeType.GLOBAL and self.scope_id is not None:
            raise ValueError("全局记忆不能填写 scope_id")
        if self.scope_type is MemoryScopeType.ASSET and self.scope_id is None:
            raise ValueError("资产范围记忆必须填写 scope_id")
        if self.source_message_id is not None and self.source_session_id is None:
            raise ValueError("来源消息存在时必须同时记录来源会话")
        if self.supersedes_id == self.id:
            raise ValueError("记忆不能替换自身")
        if self.updated_at < self.created_at:
            raise ValueError("记忆更新时间不能早于创建时间")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("记忆过期时间必须晚于创建时间")

        confirmed_statuses = {
            MemoryStatus.ACTIVE,
            MemoryStatus.SUPERSEDED,
            MemoryStatus.EXPIRED,
        }
        if self.status in confirmed_statuses and self.confirmed_at is None:
            raise ValueError("已生效、被替换或已过期的记忆必须记录确认时间")
        if self.status in {MemoryStatus.CANDIDATE, MemoryStatus.REJECTED}:
            if self.confirmed_at is not None:
                raise ValueError("候选或已拒绝记忆不能记录确认时间")
        if self.confirmed_at is not None:
            if self.confirmed_at < self.created_at or self.confirmed_at > self.updated_at:
                raise ValueError("记忆确认时间必须位于创建与更新时间之间")
        return self


class MemoryEvent(MemoryModel):
    """不保存记忆正文的审计事件。

    即使用户要求硬删除记忆内容，事件仍可保留记忆 UUID、操作类型和时间，用于证明系统执行
    过删除动作；``details`` 不允许复制 API Key、账号等敏感正文，后续 Service 会做白名单校验。
    """

    id: UUID = Field(default_factory=uuid4)
    memory_id: UUID
    event_type: MemoryEventType
    actor: MemoryActor
    details: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: datetime

    _validate_occurred_at = field_validator("occurred_at")(_require_timezone)
