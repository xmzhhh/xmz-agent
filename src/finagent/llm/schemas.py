"""与模型厂商无关的请求、响应和工具调用数据结构。

这些 Pydantic 模型位于 Agent 与具体 Provider 之间：Agent 构造 ModelRequest，
Provider 将其翻译成百炼或其他服务的请求；Provider 再把厂商响应翻译成
ModelResponse。所有外部数据都在边界处校验，避免无效状态进入 Agent 循环。
"""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """本模块所有数据模型共享的严格配置。

    ``extra="forbid"`` 会拒绝拼写错误或未声明字段；``frozen=True`` 防止对象创建后
    被意外重新赋值。Agent 的一次状态变化应该创建新对象，而不是在多个模块之间
    悄悄修改同一个请求或响应。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageRole(StrEnum):
    """模型对话中允许出现的消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    """模型结束本轮生成的标准化原因。

    各厂商使用的字符串可能不同，Provider 负责映射到这组稳定值。UNKNOWN 用于
    兼容未来新增但项目尚未识别的厂商状态，避免因为新状态导致整个请求失败。
    """

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"


class ToolCall(StrictModel):
    """模型要求应用执行的一次工具调用。

    Attributes:
        id: 本次调用的唯一标识。回传工具结果时必须携带同一个 ID。
        name: 要调用的工具名称。
        arguments: 模型生成并经 Provider 解析后的 JSON 参数对象。
    """

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    # 工具参数来自任意 JSON Schema，结构可能多层嵌套，因此这里保留 Any。
    # 真正执行工具前，还要由该工具自己的 Pydantic 输入模型再次做领域校验。
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(StrictModel):
    """提供给模型选择和调用的工具定义。

    Attributes:
        name: 在一次模型请求中唯一的工具名称。
        description: 说明工具用途、适用时机和重要边界，帮助模型正确选工具。
        parameters: 描述工具参数的 JSON Schema，根节点必须是 object。
        strict: 是否要求模型严格遵守 JSON Schema。
    """

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    description: str = Field(min_length=1, max_length=1024)
    parameters: dict[str, Any]
    strict: bool = True

    @field_validator("parameters")
    @classmethod
    def parameters_must_describe_an_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        """确保工具参数 Schema 的根节点是 JSON object。

        Function Calling 的参数最终必须是键值对。若根节点被误写为 array 或省略
        type，模型即便返回内容也无法可靠映射到 Python 函数关键字参数。
        """

        if value.get("type") != "object":
            raise ValueError("工具参数 JSON Schema 的根节点 type 必须是 object")
        return value


class Message(StrictModel):
    """Agent 与模型之间的一条标准消息。

    不同角色允许的字段组合不同，因此除了字段类型，还要在模型级校验消息状态：

    - system/user：必须包含文本，不能携带工具调用字段；
    - assistant：必须包含文本或至少一次工具调用；
    - tool：必须包含工具结果文本和对应的 tool_call_id。
    """

    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_role_specific_fields(self) -> Self:
        """校验不同消息角色允许出现的字段组合。"""

        has_content = self.content is not None and bool(self.content.strip())

        if self.role in {MessageRole.SYSTEM, MessageRole.USER}:
            if not has_content:
                raise ValueError("system 和 user 消息必须包含非空文本")
            if self.tool_calls or self.tool_call_id is not None:
                raise ValueError("system 和 user 消息不能携带工具调用字段")

        elif self.role is MessageRole.ASSISTANT:
            if not has_content and not self.tool_calls:
                raise ValueError("assistant 消息必须包含文本或工具调用")
            if self.tool_call_id is not None:
                raise ValueError("assistant 消息不能携带 tool_call_id")

        elif self.role is MessageRole.TOOL:
            if not has_content or self.tool_call_id is None:
                raise ValueError("tool 消息必须包含结果文本和 tool_call_id")
            if self.tool_calls:
                raise ValueError("tool 消息不能再次声明工具调用")

        return self


class ModelRequest(StrictModel):
    """Agent 发给任意模型 Provider 的统一请求。

    模型名称、温度等默认参数仍由 Settings 管理；这里的可选字段用于单次请求覆盖。
    ``None`` 表示本次请求沿用全局配置，而不是把默认值复制到每个调用位置。
    """

    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: Literal["auto", "none", "required"] = "auto"
    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, lt=2)
    enable_thinking: bool | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_conversation_and_tools(self) -> Self:
        """校验对话必须有用户输入，并确保工具名称不重复。"""

        if not any(message.role is MessageRole.USER for message in self.messages):
            raise ValueError("模型请求至少需要一条 user 消息")

        tool_names = [tool.name for tool in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("同一次模型请求中的工具名称不能重复")

        if self.tool_choice == "required" and not self.tools:
            raise ValueError("tool_choice=required 时必须至少提供一个工具")

        return self


class TokenUsage(StrictModel):
    """一次模型调用消耗的 token 统计。

    cached_input_tokens 和 reasoning_output_tokens 是 input/output 的细分项，已经包含在
    对应总量中，因此计算 total_tokens 时不能重复相加。
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        """返回输入与输出 token 总数。"""

        return self.input_tokens + self.output_tokens


class ModelResponse(StrictModel):
    """任意模型 Provider 返回给 Agent 的统一响应。

    Attributes:
        model: 实际完成请求的模型 ID，便于追踪模型路由和评测结果。
        content: 模型生成的最终文本；纯工具调用响应时可以为空。
        tool_calls: 模型请求应用执行的工具调用列表。
        finish_reason: 本轮生成结束原因。
        usage: 标准化 token 用量。
        response_id: 厂商返回的响应 ID，便于日志追踪和故障排查。
    """

    model: str = Field(min_length=1)
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = FinishReason.UNKNOWN
    usage: TokenUsage = Field(default_factory=TokenUsage)
    response_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def response_must_contain_an_actionable_result(self) -> Self:
        """确保响应至少包含可展示文本或可执行工具调用。"""

        has_content = self.content is not None and bool(self.content.strip())
        if not has_content and not self.tool_calls:
            raise ValueError("模型响应必须包含非空文本或至少一次工具调用")
        return self
