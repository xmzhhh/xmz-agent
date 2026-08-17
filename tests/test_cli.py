"""CLI 参数解析和终端对话循环的离线测试。

测试注入假的输入、输出和 Provider，不访问百炼，也不会等待真实键盘输入。这样可以
稳定验证多轮交互、退出命令、空输入与资源释放等终端行为。
"""

from collections.abc import Iterator

import pytest

from finagent.cli import DEFAULT_SYSTEM_PROMPT, main, run_chat
from finagent.core.config import Settings
from finagent.llm import MessageRole, ModelRequest, ModelResponse, ToolCall


class FakeProvider:
    """按顺序返回预设回答，并记录收到的请求。"""

    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """记录请求并构造最小合法响应。"""

        self.requests.append(request)
        return ModelResponse(model="fake-model", content=next(self._answers))

    async def close(self) -> None:
        """记录资源关闭动作，验证 CLI 不会泄漏连接。"""

        self.closed = True


class ToolCallingProvider:
    """先请求仓位工具、再返回自然语言回答的假 Provider。"""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """根据调用次数模拟 Function Calling 的两个模型步骤。"""

        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelResponse(
                model="fake-model",
                tool_calls=(
                    ToolCall(
                        id="call-cli-ratio",
                        name="calculate_position_ratio",
                        arguments={"position_value": 2000, "total_assets": 10000},
                    ),
                ),
            )
        return ModelResponse(model="fake-model", content="该项资产仓位为 20%。")

    async def close(self) -> None:
        """记录 CLI 是否正确释放 Provider。"""

        self.closed = True


def input_from(values: list[str]) -> Iterator[str]:
    """把预设用户输入转换成可依次读取的迭代器。"""

    return iter(values)


def test_main_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    """未提供子命令时应展示帮助，而不是静默结束或误发模型请求。"""

    main([])

    captured = capsys.readouterr()
    assert "FinAgent AI 投资研究助手" in captured.out
    assert "chat" in captured.out
    assert "dashboard" in captured.out


def test_dashboard_starts_without_llm_key_and_uses_local_defaults(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """离线面板不依赖模型 Key，默认只监听本机 127.0.0.1:8000。"""

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr("finagent.cli.get_settings", lambda: settings)

    main(["dashboard"], dashboard_runner=lambda host, port: calls.append((host, port)))

    captured = capsys.readouterr()
    assert calls == [("127.0.0.1", 8000)]
    assert "http://127.0.0.1:8000/api/v1/health" in captured.out
    assert "没有登录认证" not in captured.out


def test_dashboard_custom_lan_host_prints_security_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """显式监听所有网卡时必须提醒用户局域网读写接口没有认证。"""

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr("finagent.cli.get_settings", lambda: settings)

    main(
        ["dashboard", "--host", "0.0.0.0", "--port", "9000"],
        dashboard_runner=lambda host, port: calls.append((host, port)),
    )

    captured = capsys.readouterr()
    assert calls == [("0.0.0.0", 9000)]
    assert "没有登录认证" in captured.out
    assert "可信网络" in captured.out


def test_chat_reports_missing_llm_key_before_starting_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Settings 可以无模型 Key，但 chat 命令必须在进入交互前给出明确修复提示。"""

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr("finagent.cli.get_settings", lambda: settings)

    main(["chat"])

    captured = capsys.readouterr()
    assert "模型配置失败" in captured.out
    assert "缺少 LLM_API_KEY" in captured.out


@pytest.mark.asyncio
async def test_run_chat_keeps_multi_turn_history_and_closes_provider() -> None:
    """第二轮请求应包含第一轮问答，退出后必须关闭 Provider。"""

    provider = FakeProvider(["第一轮回答", "第二轮回答"])
    inputs = input_from(["第一个问题", "追问", "exit"])
    outputs: list[str] = []

    await run_chat(
        provider,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert len(provider.requests) == 2
    assert [message.content for message in provider.requests[1].messages] == [
        DEFAULT_SYSTEM_PROMPT,
        "第一个问题",
        "第一轮回答",
        "追问",
    ]
    assert "FinAgent：第二轮回答" in outputs
    assert provider.closed is True


@pytest.mark.asyncio
async def test_run_chat_ignores_blank_input() -> None:
    """空输入应提示用户且不调用模型，避免浪费 token。"""

    provider = FakeProvider([])
    inputs = input_from(["   ", "退出"])
    outputs: list[str] = []

    await run_chat(
        provider,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert provider.requests == []
    assert "请输入内容后再发送。" in outputs
    assert provider.closed is True


@pytest.mark.asyncio
async def test_run_chat_treats_keyboard_interrupt_as_normal_exit() -> None:
    """用户按 Ctrl+C 时应友好退出并释放资源，而不是打印异常栈。"""

    provider = FakeProvider([])
    outputs: list[str] = []

    def interrupt(_prompt: str) -> str:
        """模拟终端用户按下 Ctrl+C。"""

        raise KeyboardInterrupt

    await run_chat(provider, input_func=interrupt, output_func=outputs.append)

    assert outputs[-1] == "\n会话已结束。"
    assert provider.closed is True


@pytest.mark.asyncio
async def test_run_chat_executes_model_requested_tool() -> None:
    """CLI 应通过 Agent 完成工具回传并展示最终回答，而不是暴露中间 ToolCall。"""

    provider = ToolCallingProvider()
    inputs = input_from(["2000 元持仓占 10000 元总资产的比例是多少？", "exit"])
    outputs: list[str] = []

    await run_chat(
        provider,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].role is MessageRole.TOOL
    assert provider.requests[1].messages[-1].tool_call_id == "call-cli-ratio"
    assert "FinAgent：该项资产仓位为 20%。" in outputs
    assert provider.closed is True
