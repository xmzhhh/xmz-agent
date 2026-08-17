"""百炼 Provider 的离线单元测试。

测试使用模拟客户端，不访问网络、不消耗额度。重点验证 SDK 边界的数据翻译，防止
厂商字段变化或重构时悄悄破坏 Agent 依赖的统一请求与响应格式。
"""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from finagent.core.config import Settings
from finagent.llm import (
    BailianModelProvider,
    FinishReason,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponseError,
    ToolDefinition,
)


def make_settings() -> Settings:
    """创建不读取真实 .env 的测试配置，避免单元测试依赖个人电脑环境。"""

    return Settings(llm_api_key=SecretStr("test-key"), _env_file=None)  # type: ignore[call-arg]


def test_provider_requires_llm_key_even_with_injected_client() -> None:
    """离线 Settings 可以无 Key，但模型 Provider 创建边界必须保持严格校验。"""

    client, _ = make_client(make_text_response())
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    with pytest.raises(ValueError, match="缺少 LLM_API_KEY"):
        BailianModelProvider(settings, client)


def make_client(response: Any) -> tuple[AsyncOpenAI, AsyncMock]:
    """构造只实现本测试所需调用链的模拟 SDK 客户端。"""

    create = AsyncMock(return_value=response)
    client = MagicMock()
    client.chat.completions.create = create
    client.close = AsyncMock()
    return cast(AsyncOpenAI, client), create


def make_text_response() -> SimpleNamespace:
    """创建与 SDK 关键字段同形的最小文本响应。"""

    return SimpleNamespace(
        id="chatcmpl-test",
        model="qwen3.6-flash",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="连接成功", tool_calls=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3),
    )


@pytest.mark.asyncio
async def test_generate_translates_text_request_and_response() -> None:
    """文本请求应携带配置参数，并被还原成标准响应和 token 用量。"""

    client, create = make_client(make_text_response())
    provider = BailianModelProvider(make_settings(), client)

    result = await provider.generate(
        ModelRequest(messages=(Message(role=MessageRole.USER, content="你好"),))
    )

    assert result.content == "连接成功"
    assert result.finish_reason is FinishReason.STOP
    assert result.usage.total_tokens == 13
    assert create.await_args is not None
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "qwen3.6-flash"
    assert kwargs["messages"] == [{"role": "user", "content": "你好"}]
    assert kwargs["extra_body"] == {"enable_thinking": False}
    assert "tools" not in kwargs


@pytest.mark.asyncio
async def test_generate_translates_tool_definition() -> None:
    """提供工具时，应生成百炼 Function Calling 所需的嵌套结构。"""

    client, create = make_client(make_text_response())
    provider = BailianModelProvider(make_settings(), client)
    tool = ToolDefinition(
        name="get_price",
        description="查询资产价格",
        parameters={"type": "object", "properties": {"symbol": {"type": "string"}}},
    )

    await provider.generate(
        ModelRequest(
            messages=(Message(role=MessageRole.USER, content="查询黄金价格"),),
            tools=(tool,),
            tool_choice="required",
        )
    )

    assert create.await_args is not None
    kwargs = create.await_args.kwargs
    assert kwargs["tool_choice"] == "required"
    assert kwargs["tools"][0]["function"]["name"] == "get_price"
    assert kwargs["tools"][0]["function"]["strict"] is True


@pytest.mark.asyncio
async def test_generate_parses_tool_call() -> None:
    """模型返回的 JSON 工具参数应转换为经过校验的 Python 字典。"""

    response = SimpleNamespace(
        id="chatcmpl-tool",
        model="qwen3.6-flash",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(
                                name="get_price", arguments='{"symbol":"XAU"}'
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=8, completion_tokens=5),
    )
    client, _ = make_client(response)

    result = await BailianModelProvider(make_settings(), client).generate(
        ModelRequest(messages=(Message(role=MessageRole.USER, content="查询黄金价格"),))
    )

    assert result.finish_reason is FinishReason.TOOL_CALLS
    assert result.tool_calls[0].arguments == {"symbol": "XAU"}


@pytest.mark.asyncio
async def test_generate_rejects_invalid_tool_arguments() -> None:
    """非法工具参数必须在 Provider 边界失败，不能进入后续工具执行阶段。"""

    response = SimpleNamespace(
        id="chatcmpl-bad-tool",
        model="qwen3.6-flash",
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1",
                            function=SimpleNamespace(name="get_price", arguments="not-json"),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    client, _ = make_client(response)

    with pytest.raises(ModelResponseError, match="不是合法 JSON"):
        await BailianModelProvider(make_settings(), client).generate(
            ModelRequest(messages=(Message(role=MessageRole.USER, content="查询价格"),))
        )


@pytest.mark.asyncio
async def test_injected_client_is_not_closed() -> None:
    """外部注入的共享客户端不归 Provider 所有，关闭 Provider 时不能误关它。"""

    client, _ = make_client(make_text_response())
    provider = BailianModelProvider(make_settings(), client)

    await provider.close()

    cast(AsyncMock, client.close).assert_not_awaited()
