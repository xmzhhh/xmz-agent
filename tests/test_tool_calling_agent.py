"""最小 Agent 工具调用循环的离线单元测试。

测试使用按顺序返回响应的假 Provider，不访问百炼。重点验证消息顺序、工具结果回传、
错误自修复入口、最大步数保护，以及失败时不会污染已提交的多轮历史。
"""

import json

import pytest

from finagent.agents import AgentStepLimitError, ToolCallingAgent
from finagent.llm import (
    MessageRole,
    ModelConnectionError,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from finagent.tools import MockMarketQuoteTool, PositionRatioTool, ToolRegistry


class SequenceProvider:
    """按顺序返回预设响应或异常，并记录每次模型请求。"""

    def __init__(self, outcomes: list[ModelResponse | Exception]) -> None:
        self._outcomes = iter(outcomes)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """记录请求，并返回本步骤预设结果。"""

        self.requests.append(request)
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        """记录关闭动作，验证 Agent 生命周期管理。"""

        self.closed = True


def make_registry() -> ToolRegistry:
    """构造包含本阶段两个工具的测试注册中心。"""

    return ToolRegistry((MockMarketQuoteTool(), PositionRatioTool()))


@pytest.mark.asyncio
async def test_agent_executes_tool_and_returns_final_answer() -> None:
    """模型先请求工具再回答时，Agent 应按协议补齐 assistant/tool 消息并继续调用模型。"""

    tool_call = ToolCall(
        id="call-ratio",
        name="calculate_position_ratio",
        arguments={"position_value": 3000, "total_assets": 10000},
    )
    provider = SequenceProvider(
        [
            ModelResponse(model="fake-model", tool_calls=(tool_call,)),
            ModelResponse(model="fake-model", content="该项资产仓位为 30%。"),
        ]
    )
    agent = ToolCallingAgent(provider, make_registry(), "你是测试投资助手")

    answer = await agent.ask("我的仓位是多少？")

    assert answer == "该项资产仓位为 30%。"
    assert len(provider.requests) == 2
    assert {tool.name for tool in provider.requests[0].tools} == {
        "get_mock_market_quote",
        "calculate_position_ratio",
    }

    second_request = provider.requests[1]
    assert [message.role for message in second_request.messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    assert second_request.messages[2].tool_calls == (tool_call,)
    assert second_request.messages[3].tool_call_id == "call-ratio"
    tool_content = second_request.messages[3].content
    assert tool_content is not None
    assert json.loads(tool_content)["data"]["position_ratio_percent"] == 30.0

    # 成功后正式历史还应包含模型最终回答，供下一轮追问继续使用。
    assert agent.messages[-1].content == "该项资产仓位为 30%。"


@pytest.mark.asyncio
async def test_agent_returns_tool_error_to_model_for_correction() -> None:
    """非法工具参数应转换成 tool 错误消息，让模型有机会解释或修正，而非直接崩溃。"""

    invalid_call = ToolCall(
        id="call-invalid",
        name="calculate_position_ratio",
        arguments={"position_value": 12000, "total_assets": 10000},
    )
    provider = SequenceProvider(
        [
            ModelResponse(model="fake-model", tool_calls=(invalid_call,)),
            ModelResponse(model="fake-model", content="持仓市值不能大于总资产，请检查输入。"),
        ]
    )
    agent = ToolCallingAgent(provider, make_registry(), "你是测试投资助手")

    answer = await agent.ask("帮我算仓位")

    assert answer == "持仓市值不能大于总资产，请检查输入。"
    error_content = provider.requests[1].messages[-1].content
    assert error_content is not None
    parsed_error = json.loads(error_content)
    assert parsed_error["ok"] is False
    assert parsed_error["error_type"] == "ToolValidationError"
    assert "持仓市值不能大于账户总资产" in parsed_error["error"]


@pytest.mark.asyncio
async def test_agent_step_limit_stops_before_last_requested_tool_execution() -> None:
    """模型持续请求工具时必须在上限处停止，防止死循环和未来工具的重复副作用。"""

    repeated_call = ToolCall(
        id="call-loop",
        name="get_mock_market_quote",
        arguments={"asset": "gold"},
    )
    provider = SequenceProvider(
        [
            ModelResponse(model="fake-model", tool_calls=(repeated_call,)),
            ModelResponse(model="fake-model", tool_calls=(repeated_call,)),
        ]
    )
    agent = ToolCallingAgent(
        provider,
        make_registry(),
        "你是测试投资助手",
        max_steps=2,
    )

    with pytest.raises(AgentStepLimitError, match="2 次模型调用"):
        await agent.ask("不断查询黄金")

    assert len(provider.requests) == 2
    # 本轮没有完整结束，因此正式历史仍只有 system 消息。
    assert [message.role for message in agent.messages] == [MessageRole.SYSTEM]


@pytest.mark.asyncio
async def test_provider_failure_does_not_commit_partial_agent_history() -> None:
    """任意模型步骤发生网络错误后，都不能把半截用户消息写入后续上下文。"""

    provider = SequenceProvider([ModelConnectionError("模拟网络失败")])
    agent = ToolCallingAgent(provider, make_registry(), "你是测试投资助手")

    with pytest.raises(ModelConnectionError, match="模拟网络失败"):
        await agent.ask("这次会失败")

    assert [message.role for message in agent.messages] == [MessageRole.SYSTEM]


def test_agent_rejects_invalid_step_limit() -> None:
    """非正数步数无法形成有效循环，应在初始化阶段立即拒绝。"""

    with pytest.raises(ValueError, match="max_steps"):
        ToolCallingAgent(
            SequenceProvider([]),
            make_registry(),
            "你是测试投资助手",
            max_steps=0,
        )
