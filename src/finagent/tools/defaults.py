"""项目默认工具集合的组装入口。

CLI、检查脚本和未来 Web API 都应从同一工厂取得默认工具，避免某个入口新增了工具，
另一个入口却忘记同步。工厂每次返回新的注册中心，防止不同会话意外共享可变状态。
"""

from finagent.tools.investment import MockMarketQuoteTool, PositionRatioTool
from finagent.tools.registry import ToolRegistry


def create_default_tool_registry() -> ToolRegistry:
    """创建当前阶段允许 Agent 使用的默认工具注册中心。"""

    return ToolRegistry((MockMarketQuoteTool(), PositionRatioTool()))
