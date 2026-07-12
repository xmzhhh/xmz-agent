"""工具注册中心。

注册中心是 Agent 与具体工具之间的唯一入口：它负责保存可用工具、阻止名称冲突、向
模型提供工具定义，并把模型返回的 ``ToolCall`` 分派给正确实现。未来增加权限、超时、
审计日志或 MCP 工具时，可以在这里统一扩展，而不必修改每个 Agent。
"""

from typing import Any

from finagent.llm import ToolCall, ToolDefinition
from finagent.tools.base import BaseTool, ToolResult
from finagent.tools.errors import DuplicateToolError, ToolNotFoundError


class ToolRegistry:
    """集中管理本次 Agent 会话允许使用的工具集合。"""

    def __init__(self, tools: tuple[BaseTool[Any], ...] = ()) -> None:
        """创建注册中心，并按顺序注册初始工具。

        Args:
            tools: 初始工具元组。使用不可变元组可以明确调用者传入的是配置快照。

        Raises:
            DuplicateToolError: 初始工具中存在同名项。
        """

        self._tools: dict[str, BaseTool[Any]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseTool[Any]) -> None:
        """注册一个工具，并拒绝覆盖已经存在的同名实现。"""

        if tool.name in self._tools:
            raise DuplicateToolError(f"工具名称已注册：{tool.name}")
        self._tools[tool.name] = tool

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回全部工具定义，供 Agent 构造 ``ModelRequest``。"""

        return tuple(tool.definition for tool in self._tools.values())

    def get(self, name: str) -> BaseTool[Any]:
        """按名称取得工具；名称不存在时抛出稳定的工具层异常。"""

        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolNotFoundError(f"未注册的工具：{name}") from error

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """根据模型产生的工具调用，查找并执行对应工具。"""

        tool = self.get(tool_call.name)
        return await tool.execute(tool_call.arguments)
