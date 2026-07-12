"""工具抽象层、注册中心和第一批投资工具的单元测试。

这些测试全部使用本地确定性数据，不访问百炼或真实行情接口。重点验证模型生成的不可信
参数会在边界处被拒绝、工具名称能被正确分派、数值计算结果稳定且模拟数据标记不会
丢失。
"""

import json
from typing import Any

import pytest

from finagent.llm import ToolCall
from finagent.tools import (
    DuplicateToolError,
    MockMarketQuoteTool,
    PositionRatioTool,
    ToolNotFoundError,
    ToolRegistry,
    ToolValidationError,
)


def make_registry() -> ToolRegistry:
    """构造包含两个教学工具的注册中心，供多个场景复用。"""

    return ToolRegistry((MockMarketQuoteTool(), PositionRatioTool()))


def get_property_schema(properties: object, property_name: str) -> dict[str, Any]:
    """从 JSON Schema 的 properties 中安全取得指定字段，避免测试中散布类型忽略。"""

    assert isinstance(properties, dict)
    property_schema = properties[property_name]
    assert isinstance(property_schema, dict)
    return property_schema


def test_tool_definition_is_generated_from_pydantic_input_model() -> None:
    """输入模型应自动变成 JSON Schema，防止手写 Schema 与 Python 校验规则失配。"""

    definition = PositionRatioTool().definition

    assert definition.name == "calculate_position_ratio"
    assert definition.parameters["type"] == "object"
    assert set(definition.parameters["required"]) == {"position_value", "total_assets"}
    properties = definition.parameters["properties"]
    total_assets_schema = get_property_schema(properties, "total_assets")
    assert total_assets_schema["exclusiveMinimum"] == 0
    assert definition.parameters["additionalProperties"] is False


@pytest.mark.asyncio
async def test_registry_dispatches_tool_call_by_name() -> None:
    """注册中心应把模型的工具调用准确分派给对应实现，防止执行错工具。"""

    result = await make_registry().execute(
        ToolCall(
            id="call-1",
            name="calculate_position_ratio",
            arguments={"position_value": 3000, "total_assets": 10000},
        )
    )

    assert result.tool_name == "calculate_position_ratio"
    assert result.data["position_ratio_percent"] == 30.0
    assert result.data["remaining_ratio_percent"] == 70.0


@pytest.mark.asyncio
async def test_mock_quote_is_explicitly_marked_as_non_realtime_data() -> None:
    """模拟行情必须携带来源和模拟标记，防止演示数据被误认为实时投资依据。"""

    result = await MockMarketQuoteTool().execute({"asset": "gold"})

    assert result.data["asset_name"] == "黄金"
    assert result.data["is_mock"] is True
    assert result.data["source"] == "FinAgent 内置教学数据"


@pytest.mark.asyncio
async def test_tool_rejects_unknown_arguments_from_model() -> None:
    """模型臆造额外参数时应拒绝执行，防止未知字段被静默忽略。"""

    with pytest.raises(ToolValidationError, match="参数校验失败"):
        await MockMarketQuoteTool().execute({"asset": "gold", "period": "1d"})


@pytest.mark.asyncio
async def test_tool_rejects_unsupported_asset_code() -> None:
    """不在白名单中的资产不能进入工具逻辑，防止返回无来源或错误行情。"""

    with pytest.raises(ToolValidationError, match="参数校验失败"):
        await MockMarketQuoteTool().execute({"asset": "bitcoin"})


@pytest.mark.asyncio
async def test_position_ratio_rejects_position_larger_than_total_assets() -> None:
    """持仓大于总资产属于业务矛盾，应在计算前给出参数错误。"""

    with pytest.raises(ToolValidationError, match="持仓市值不能大于账户总资产"):
        await PositionRatioTool().execute({"position_value": 12000, "total_assets": 10000})


@pytest.mark.asyncio
async def test_position_ratio_rejects_zero_total_assets() -> None:
    """总资产为零会导致除零错误，应由输入模型提前阻止。"""

    with pytest.raises(ToolValidationError, match="greater_than"):
        await PositionRatioTool().execute({"position_value": 0, "total_assets": 0})


def test_registry_rejects_duplicate_tool_names() -> None:
    """重名工具会让模型调用产生歧义，注册阶段就必须失败。"""

    with pytest.raises(DuplicateToolError, match="工具名称已注册"):
        ToolRegistry((MockMarketQuoteTool(), MockMarketQuoteTool()))


@pytest.mark.asyncio
async def test_registry_reports_unregistered_tool() -> None:
    """模型请求未开放工具时应返回可识别异常，不能暴露原始 KeyError。"""

    with pytest.raises(ToolNotFoundError, match="未注册的工具"):
        await make_registry().execute(
            ToolCall(id="call-unknown", name="delete_account", arguments={})
        )


@pytest.mark.asyncio
async def test_tool_result_can_be_serialized_for_future_tool_message() -> None:
    """标准结果必须能序列化成 JSON，下一阶段才能安全放入 role=tool 消息。"""

    result = await PositionRatioTool().execute({"position_value": 2500, "total_assets": 10000})
    message_content = result.to_message_content()

    assert json.loads(message_content) == {
        "ok": True,
        "tool_name": "calculate_position_ratio",
        "data": {
            "position_value": 2500.0,
            "total_assets": 10000.0,
            "position_ratio_percent": 25.0,
            "remaining_ratio_percent": 75.0,
        },
    }
