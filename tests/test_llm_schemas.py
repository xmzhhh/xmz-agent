"""模型交互数据结构的单元测试。

测试重点不是某一家模型 API，而是项目自身协议层能否阻止无效消息、重复工具、
非法采样参数和空响应进入 Agent 循环。这些测试以后切换模型厂商时仍应保持通过。
"""

import pytest
from pydantic import ValidationError

from finagent.llm.schemas import (
    FinishReason,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)


def make_quote_tool() -> ToolDefinition:
    """构造可在多个测试中复用的模拟行情工具定义。"""

    return ToolDefinition(
        name="get_quote",
        description="查询指定资产的模拟行情",
        parameters={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    )


def test_user_message_requires_non_empty_content() -> None:
    """空白用户问题无法驱动 Agent，应在进入 Provider 前被拒绝。"""

    with pytest.raises(ValidationError, match="必须包含非空文本"):
        Message(role=MessageRole.USER, content="   ")


def test_tool_message_requires_matching_call_id() -> None:
    """工具结果缺少调用 ID 时无法与请求配对，因此必须拒绝。"""

    with pytest.raises(ValidationError, match="tool_call_id"):
        Message(role=MessageRole.TOOL, content='{"price": 100}')


def test_assistant_message_can_contain_only_tool_calls() -> None:
    """模型决定调用工具时可以暂时没有自然语言文本。"""

    call = ToolCall(id="call-1", name="get_quote", arguments={"symbol": "GLD"})
    message = Message(role=MessageRole.ASSISTANT, tool_calls=(call,))

    assert message.content is None
    assert message.tool_calls == (call,)


def test_tool_name_must_be_a_valid_identifier() -> None:
    """包含空格的工具名无法稳定映射到 Python 函数，应被 Schema 拒绝。"""

    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        ToolCall(id="call-1", name="get quote", arguments={})


def test_tool_parameters_must_use_object_schema() -> None:
    """Function Calling 参数根节点必须是 object，不能直接定义为数组。"""

    with pytest.raises(ValidationError, match="根节点 type 必须是 object"):
        ToolDefinition(
            name="get_quote",
            description="查询模拟行情",
            parameters={"type": "array", "items": {"type": "string"}},
        )


def test_model_request_requires_a_user_message() -> None:
    """只有系统指令而没有用户输入时，不应发起本轮模型请求。"""

    with pytest.raises(ValidationError, match="至少需要一条 user 消息"):
        ModelRequest(messages=(Message(role=MessageRole.SYSTEM, content="你是投资助手"),))


def test_model_request_rejects_duplicate_tool_names() -> None:
    """重复工具名会让模型调用无法唯一分派到实现，必须提前拒绝。"""

    tool = make_quote_tool()
    user_message = Message(role=MessageRole.USER, content="查询黄金行情")

    with pytest.raises(ValidationError, match="工具名称不能重复"):
        ModelRequest(messages=(user_message,), tools=(tool, tool))


def test_required_tool_choice_needs_at_least_one_tool() -> None:
    """要求模型必须调用工具时，请求中必须实际提供工具。"""

    user_message = Message(role=MessageRole.USER, content="查询行情")

    with pytest.raises(ValidationError, match="必须至少提供一个工具"):
        ModelRequest(messages=(user_message,), tool_choice="required")


def test_token_usage_calculates_total_without_double_counting_details() -> None:
    """缓存和推理 token 是细分项，不应在总量中重复累加。"""

    usage = TokenUsage(
        input_tokens=100,
        output_tokens=40,
        cached_input_tokens=20,
        reasoning_output_tokens=10,
    )

    assert usage.total_tokens == 140


def test_token_usage_rejects_negative_values() -> None:
    """厂商适配器若产生负 token 数，说明映射错误，应立即暴露。"""

    with pytest.raises(ValidationError, match="greater_than_equal"):
        TokenUsage(input_tokens=-1)


def test_model_response_requires_text_or_tool_call() -> None:
    """既没有文本也没有工具调用的响应无法推进 Agent 状态。"""

    with pytest.raises(ValidationError, match="必须包含非空文本或至少一次工具调用"):
        ModelResponse(model="qwen3.6-flash")


def test_valid_model_response_keeps_trace_information() -> None:
    """标准响应应保留模型、结束原因、用量和响应 ID，便于后续评测。"""

    response = ModelResponse(
        model="qwen3.6-flash",
        content="当前模拟金价为 100 元。",
        finish_reason=FinishReason.STOP,
        usage=TokenUsage(input_tokens=20, output_tokens=10),
        response_id="response-1",
    )

    assert response.content == "当前模拟金价为 100 元。"
    assert response.finish_reason is FinishReason.STOP
    assert response.usage.total_tokens == 30
    assert response.response_id == "response-1"
