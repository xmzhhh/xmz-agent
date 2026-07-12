"""工具的抽象基类、输入模型与标准执行结果。

本模块位于模型协议层和具体业务工具之间。具体工具只需声明名称、描述、Pydantic 输入
模型，并实现核心业务逻辑；基类统一负责生成 Function Calling 所需的 JSON Schema、
校验不可信参数以及转换异常。这样可以避免每个工具重复编写边界处理代码。
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from finagent.llm import ToolDefinition
from finagent.tools.errors import ToolError, ToolExecutionError, ToolValidationError


class ToolInput(BaseModel):
    """所有工具输入模型共享的配置。

    ``extra="forbid"`` 会拒绝模型臆造的多余参数，防止拼写错误被静默忽略；冻结对象则
    保证参数通过校验后不会在执行过程中被其他代码意外修改。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolResult(BaseModel):
    """一次工具成功执行后的标准结果。

    Attributes:
        tool_name: 实际执行的工具名称，便于日志、回放与审计。
        data: 可被序列化为 JSON 的业务结果。当前用 ``Any`` 保留不同工具的扩展能力，
            具体工具仍应只返回 JSON 兼容的值。

    下一阶段会把该对象序列化后放入 ``role=tool`` 的消息，再交还给大模型总结。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    data: dict[str, Any]

    def to_message_content(self) -> str:
        """生成可放入模型 tool 消息的紧凑 JSON 文本。"""

        return self.model_dump_json()


class BaseTool[InputT: ToolInput](ABC):
    """所有可由 Agent 调用的工具基类。

    子类通过类属性描述工具，通过 ``run`` 实现已经校验后的核心逻辑。公开的
    ``execute`` 方法是唯一执行入口，调用者不能绕开参数校验直接运行工具。
    """

    name: str
    description: str
    input_model: type[InputT]

    @property
    def definition(self) -> ToolDefinition:
        """把 Python 输入模型转换成模型可理解的 Function Calling 定义。"""

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.input_model.model_json_schema(),
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """校验模型参数并执行工具。

        Args:
            arguments: Provider 从模型 Function Calling 响应中解析出的参数字典。

        Returns:
            带工具名称的标准化成功结果。

        Raises:
            ToolValidationError: 参数缺失、类型错误、超出范围或包含未知字段。
            ToolExecutionError: 参数合法，但具体工具运行时发生非预期错误。
            ToolError: 具体工具主动抛出的更明确工具异常会原样透传。

        工具统一设计为异步接口，是因为后续行情、新闻和数据库工具大多包含 I/O。
        即使当前两个教学工具只是本地计算，也保持同一接口，避免 Agent 层区分同步工具
        和异步工具。
        """

        try:
            validated_input = self.input_model.model_validate(arguments)
        except ValidationError as error:
            # 不把整段 Pydantic 堆栈交给模型，只保留可理解的校验摘要。
            raise ToolValidationError(f"工具 {self.name} 的参数校验失败：{error}") from error

        try:
            data = await self.run(validated_input)
        except ToolError:
            # 子类若已经给出工具领域异常，应保留其准确类型和错误信息。
            raise
        except Exception as error:
            # 第三方 SDK 可能抛出任意异常；统一包装后 Agent 不必依赖具体 SDK。
            raise ToolExecutionError(f"工具 {self.name} 执行失败：{error}") from error

        return ToolResult(tool_name=self.name, data=data)

    @abstractmethod
    async def run(self, tool_input: InputT) -> dict[str, Any]:
        """执行已经通过校验的核心业务逻辑，由每个具体工具实现。"""
