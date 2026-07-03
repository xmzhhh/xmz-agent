"""核心多轮会话状态的单元测试。

这里不测试终端界面，只验证成功回答如何更新历史，以及失败时历史是否保持一致。
"""

import pytest

from finagent.core.chat import ChatSession
from finagent.llm import ModelConnectionError, ModelRequest, ModelResponse


class RecordingProvider:
    """记录请求并可选择返回文本或抛出连接错误的测试 Provider。"""

    def __init__(self, *, answer: str = "测试回答", should_fail: bool = False) -> None:
        self.answer = answer
        self.should_fail = should_fail
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """根据测试场景返回响应或模拟网络失败。"""

        self.requests.append(request)
        if self.should_fail:
            raise ModelConnectionError("模拟连接失败")
        return ModelResponse(model="fake-model", content=self.answer)

    async def close(self) -> None:
        """测试对象没有真实网络资源，因此无需处理。"""


@pytest.mark.asyncio
async def test_ask_adds_successful_turn_to_history() -> None:
    """成功问答后，历史应依次包含 system、user 和 assistant 消息。"""

    session = ChatSession(RecordingProvider(), "你是测试助手")

    answer = await session.ask(" 你好 ")

    assert answer == "测试回答"
    assert [message.content for message in session.messages] == [
        "你是测试助手",
        "你好",
        "测试回答",
    ]


@pytest.mark.asyncio
async def test_failed_request_does_not_pollute_history() -> None:
    """网络失败不能留下孤立用户消息，否则下一次请求会得到不完整上下文。"""

    session = ChatSession(RecordingProvider(should_fail=True), "你是测试助手")

    with pytest.raises(ModelConnectionError):
        await session.ask("这次会失败")

    assert [message.content for message in session.messages] == ["你是测试助手"]


@pytest.mark.asyncio
async def test_ask_rejects_blank_input_before_calling_provider() -> None:
    """空输入应在会话层被拒绝，保证其他界面复用时也不会浪费模型请求。"""

    provider = RecordingProvider()
    session = ChatSession(provider, "你是测试助手")

    with pytest.raises(ValueError, match="用户输入不能为空"):
        await session.ask("   ")

    assert provider.requests == []
