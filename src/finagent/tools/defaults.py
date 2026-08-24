"""项目默认工具集合的组装入口。

CLI、检查脚本和未来 Web API 都应从同一工厂取得默认工具，避免某个入口新增了工具，
另一个入口却忘记同步。工厂每次返回新的注册中心，防止不同会话意外共享可变状态。
"""

from finagent.tools.asset_read import (
    HoldingRecordTool,
    LedgerReadService,
    PortfolioReadService,
    PortfolioSnapshotTool,
    TransactionLedgerSummaryTool,
)
from finagent.tools.investment import MockMarketQuoteTool, PositionRatioTool
from finagent.tools.registry import ToolRegistry


def create_default_tool_registry() -> ToolRegistry:
    """创建当前阶段允许 Agent 使用的默认工具注册中心。"""

    return ToolRegistry((MockMarketQuoteTool(), PositionRatioTool()))


def create_read_only_asset_tool_registry(
    portfolio_service: PortfolioReadService,
    ledger_service: LedgerReadService,
) -> ToolRegistry:
    """创建只允许查询当前资产与交易事实的 Agent 工具注册中心。

    本工厂故意不混入 Phase 1 的模拟行情工具，也不注册 Dashboard/Transaction Service 的任何
    写方法。调用方若需要其他无副作用工具，应在组合根中显式构造新的白名单，而不能让模型通过
    方法名动态访问 Service。
    """

    return ToolRegistry(
        (
            PortfolioSnapshotTool(portfolio_service),
            HoldingRecordTool(portfolio_service),
            TransactionLedgerSummaryTool(ledger_service),
        )
    )
