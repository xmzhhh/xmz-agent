"""从一轮已完成对话中抽取长期记忆候选。

本模块只负责把模型输出约束成结构化的候选草稿，不写数据库，也不决定候选是否生效。
真正的来源校验、敏感信息拦截和状态迁移仍由 :class:`MemoryService` 完成，因此模型即使
输出错误或恶意 JSON，也不能绕过“用户确认后才能成为 ACTIVE 记忆”的业务边界。
"""

import json
from typing import Protocol, Self

from pydantic import Field, JsonValue, ValidationError, field_validator, model_validator

from finagent.llm import Message, MessageRole, ModelProvider, ModelRequest
from finagent.memory.errors import MemoryExtractionError
from finagent.memory.models import (
    ConversationMessage,
    MemoryModel,
    MemoryScopeType,
    MemoryType,
)


class MemoryCandidateDraft(MemoryModel):
    """候选抽取器能够提出、但不能直接激活的一条记忆草稿。

    与 ``MemoryCandidateCreate`` 相比，本模型故意不包含来源 UUID、状态、确认时间和版本。
    来源必须由可信的应用服务根据刚刚持久化的 user 消息补充，避免模型伪造来源关系。
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
    ttl_seconds: int | None = Field(default=None, ge=1, le=31_536_000)

    _normalize_scope_id = field_validator("scope_id", mode="before")(
        lambda value: value.strip().upper() if isinstance(value, str) else value
    )

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """保证全局记忆和资产记忆的范围字段组合合法。"""

        if self.scope_type is MemoryScopeType.GLOBAL and self.scope_id is not None:
            raise ValueError("全局候选草稿不能填写 scope_id")
        if self.scope_type is MemoryScopeType.ASSET and self.scope_id is None:
            raise ValueError("资产候选草稿必须填写 scope_id")
        return self


class MemoryCandidateExtraction(MemoryModel):
    """模型候选抽取响应的唯一合法根结构。"""

    candidate: MemoryCandidateDraft | None


class MemoryCandidateExtractor(Protocol):
    """候选抽取器的最小异步协议，便于测试时替换为确定性 Fake。"""

    async def extract(
        self,
        user_message: ConversationMessage,
        assistant_answer: str,
    ) -> MemoryCandidateDraft | None:
        """从一轮问答中返回至多一条候选；没有稳定信息时返回 ``None``。"""

        ...


class ModelMemoryCandidateExtractor:
    """通过统一 ``ModelProvider`` 生成严格 JSON 候选。

    Provider 生命周期由应用组合根统一管理。本适配器禁用工具、关闭随机采样，并且只允许
    一轮产生一条候选，以保持确认页面清晰，也避免一次模型输出造成部分候选写入成功。
    """

    _SYSTEM_PROMPT = (
        "你是 FinAgent 的长期记忆候选抽取器。仅提取用户明确表达、未来多轮仍有用的"
        "偏好、约束、关注项、目标或反馈。不要保存当前持仓、价格、收益、交易流水、"
        "身份证明、账户信息、API Key、密码或其他敏感信息；不要根据助手回答推测用户事实。"
        "每轮最多返回一条候选。没有合适内容时返回 {\"candidate\":null}。有候选时严格"
        "返回 {\"candidate\":{\"memory_type\":\"preference|constraint|watchlist|goal|feedback\","
        "\"memory_key\":\"稳定的英文键\",\"value\":{...},\"scope_type\":\"global|asset\","
        "\"scope_id\":null或资产代码,\"ttl_seconds\":null或正整数}}。只输出 JSON。"
    )

    def __init__(
        self,
        provider: ModelProvider,
        *,
        max_output_tokens: int = 600,
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens 必须大于 0")
        self._provider = provider
        self._max_output_tokens = max_output_tokens

    async def extract(
        self,
        user_message: ConversationMessage,
        assistant_answer: str,
    ) -> MemoryCandidateDraft | None:
        """提交最小问答事实，并把返回值校验为候选草稿。

        Raises:
            ValueError: 来源不是 user 消息，或助手最终回答为空。
            MemoryExtractionError: 模型返回工具调用、空文本、非法 JSON 或不合法字段。
            ModelProviderError: Provider 本身调用失败，由上层决定是否降级。
        """

        if user_message.role is not MessageRole.USER:
            raise ValueError("候选记忆只能从 user 消息抽取")
        normalized_answer = assistant_answer.strip()
        if not normalized_answer:
            raise ValueError("助手最终回答不能为空")

        payload = {
            "user_message": user_message.content,
            # 助手回答只帮助理解指代关系；系统提示已明确禁止把它当成用户事实来源。
            "assistant_answer": normalized_answer,
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
            raise MemoryExtractionError("候选记忆抽取不允许返回工具调用")
        if response.content is None or not response.content.strip():
            raise MemoryExtractionError("候选记忆抽取没有返回 JSON")

        try:
            raw_result = json.loads(response.content)
            extraction = MemoryCandidateExtraction.model_validate(raw_result)
        except (json.JSONDecodeError, ValidationError, TypeError) as error:
            # 不把模型原文拼进异常，避免响应中意外回显敏感数据。
            raise MemoryExtractionError("候选记忆抽取结果不符合结构化协议") from error
        return extraction.candidate
