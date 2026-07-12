"""第一批可离线运行的投资领域教学工具。

这里故意使用固定模拟行情，而不连接真实金融数据源：本阶段要验证的是工具协议、参数
校验和分派流程。模拟数据会明确标注 ``is_mock=True``，防止被用户或模型误当成实时
行情。下一阶段可以新增真实数据适配器，而无需改变工具注册中心的接口。
"""

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from finagent.tools.base import BaseTool, ToolInput

type AssetCode = Literal["gold", "csi300", "nasdaq100"]


class MockMarketQuoteInput(ToolInput):
    """模拟行情工具的输入参数。"""

    asset: AssetCode = Field(
        description="资产代码：gold 表示黄金，csi300 表示沪深300，nasdaq100 表示纳斯达克100"
    )


class MockMarketQuoteTool(BaseTool[MockMarketQuoteInput]):
    """返回固定模拟行情，用于验证 Agent 是否能正确选择和调用工具。"""

    name = "get_mock_market_quote"
    description = (
        "查询黄金、沪深300或纳斯达克100的模拟行情。仅用于开发测试，结果不是实时数据，"
        "不得据此作出真实投资决策。"
    )
    input_model = MockMarketQuoteInput

    _QUOTES: dict[AssetCode, dict[str, Any]] = {
        "gold": {
            "asset_name": "黄金",
            "price": 2380.5,
            "currency": "USD",
            "unit": "每盎司",
            "change_percent": 0.42,
        },
        "csi300": {
            "asset_name": "沪深300",
            "price": 3925.8,
            "currency": "CNY",
            "unit": "指数点",
            "change_percent": -0.31,
        },
        "nasdaq100": {
            "asset_name": "纳斯达克100",
            "price": 21840.2,
            "currency": "USD",
            "unit": "指数点",
            "change_percent": 0.76,
        },
    }

    async def run(self, tool_input: MockMarketQuoteInput) -> dict[str, Any]:
        """读取确定性的本地样例，并附加显眼的模拟数据标记。"""

        return {
            "asset_code": tool_input.asset,
            **self._QUOTES[tool_input.asset],
            "is_mock": True,
            "source": "FinAgent 内置教学数据",
        }


class PositionRatioInput(ToolInput):
    """仓位比例计算工具的输入参数。"""

    position_value: float = Field(ge=0, allow_inf_nan=False, description="某项持仓当前市值")
    total_assets: float = Field(gt=0, allow_inf_nan=False, description="账户总资产")

    @model_validator(mode="after")
    def position_cannot_exceed_total_assets(self) -> Self:
        """拒绝持仓市值大于总资产的矛盾输入。"""

        if self.position_value > self.total_assets:
            raise ValueError("持仓市值不能大于账户总资产")
        return self


class PositionRatioTool(BaseTool[PositionRatioInput]):
    """使用确定性公式计算单项资产占总资产的仓位比例。"""

    name = "calculate_position_ratio"
    description = (
        "根据某项持仓市值和账户总资产计算仓位占比。涉及金额比例时应调用此工具，"
        "不要让大模型自行心算。"
    )
    input_model = PositionRatioInput

    async def run(self, tool_input: PositionRatioInput) -> dict[str, Any]:
        """计算持仓和剩余资产百分比，并统一保留两位小数。"""

        ratio = tool_input.position_value / tool_input.total_assets * 100
        return {
            "position_value": tool_input.position_value,
            "total_assets": tool_input.total_assets,
            "position_ratio_percent": round(ratio, 2),
            "remaining_ratio_percent": round(100 - ratio, 2),
        }
