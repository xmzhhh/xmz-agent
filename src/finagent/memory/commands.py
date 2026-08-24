"""MemoryService 接收的安全命令模型与确定性结果。

模型只能产生 :class:`MemoryCandidateCreate`，其中故意没有 ``status``、``confirmed_at`` 和
``version`` 字段，因此即使模型输出被完整解析，也不能绕过 Service 把自己生成的内容直接标记
为长期有效记忆。
"""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from finagent.memory.models import (
    MemoryItem,
    MemoryModel,
    MemoryScopeType,
    MemoryType,
)


class MemoryCandidateCreate(MemoryModel):
    """模型或确定性抽取器提出的一条待用户确认记忆。

    ``ttl_seconds=None`` 表示没有自动过期时间，适合稳定偏好；临时目标或关注项可设置最多一年
    的 TTL。来源会话和消息必须同时存在，Service 还会确认该消息确实属于 user。
    """

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
    source_session_id: UUID
    source_message_id: UUID
    ttl_seconds: int | None = Field(default=None, ge=1, le=31_536_000)

    _normalize_scope_id = field_validator("scope_id", mode="before")(
        lambda value: value.strip().upper() if isinstance(value, str) else value
    )

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """保证全局和资产范围的代码组合无歧义。"""

        if self.scope_type is MemoryScopeType.GLOBAL and self.scope_id is not None:
            raise ValueError("全局候选记忆不能填写 scope_id")
        if self.scope_type is MemoryScopeType.ASSET and self.scope_id is None:
            raise ValueError("资产候选记忆必须填写 scope_id")
        return self


class MemoryRejectionReason(StrEnum):
    """拒绝候选时允许进入审计日志的非敏感原因代码。"""

    USER_DECISION = "user_decision"
    INCORRECT = "incorrect"
    NOT_USEFUL = "not_useful"
    DUPLICATE = "duplicate"


class MemoryConfirmationResult(MemoryModel):
    """确认候选后的新 ACTIVE 记忆及可选旧版本。"""

    memory: MemoryItem
    superseded_memory: MemoryItem | None = None
