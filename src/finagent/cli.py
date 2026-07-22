"""FinAgent 命令行入口。

CLI 只负责解析命令、接收用户输入和展示结果；多轮历史与工具循环由
``ToolCallingAgent`` 管理，模型厂商差异由 Provider 管理。保持入口层轻量，可以避免
界面代码与核心业务相互耦合。
"""

import argparse
import asyncio
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from finagent.agents import AgentError, ToolCallingAgent
from finagent.core.config import get_settings
from finagent.llm import (
    BailianModelProvider,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelProvider,
    ModelProviderError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from finagent.tools import ToolRegistry, create_default_tool_registry
from finagent.web.server import run_dashboard_server

DEFAULT_SYSTEM_PROMPT = (
    "你是 FinAgent，一名谨慎、客观的 AI 投资研究助手。"
    "你需要区分事实、推测和观点，不承诺收益，也不替用户做最终投资决定。"
    "涉及行情查询和数值计算时优先使用已提供工具，不要编造工具结果。"
    "模拟行情必须明确告知用户不是实时数据，不能作为真实投资依据。"
)
EXIT_COMMANDS = {"exit", "quit", "退出"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

type DashboardRunner = Callable[[str, int], None]


def _valid_port(value: str) -> int:
    """把命令行端口转换为 1～65535 的整数。"""

    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("端口必须是整数") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("端口必须位于 1～65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器，集中维护所有子命令及帮助文本。"""

    parser = argparse.ArgumentParser(prog="finagent", description="FinAgent AI 投资研究助手")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("chat", help="启动与千问模型的多轮对话")
    dashboard_parser = subparsers.add_parser("dashboard", help="启动本地资产面板 API")
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    dashboard_parser.add_argument("--port", type=_valid_port, default=8000, help="监听端口")
    return parser


def format_provider_error(error: ModelProviderError) -> str:
    """把内部异常转换成适合终端用户理解且不泄露敏感信息的提示。"""

    if isinstance(error, ModelAuthenticationError):
        return "模型鉴权失败，请检查 .env 中的 API Key 和模型权限。"
    if isinstance(error, ModelRateLimitError):
        return "模型请求受到限流或额度不足，请稍后重试并检查百炼额度。"
    if isinstance(error, ModelTimeoutError):
        return "模型响应超时，请检查网络后重试。"
    if isinstance(error, ModelConnectionError):
        return "无法连接模型服务，请检查网络、代理或 TLS 设置。"
    return f"模型调用失败：{error}"


async def run_chat(
    provider: ModelProvider,
    *,
    registry: ToolRegistry | None = None,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> None:
    """运行支持工具调用的多轮终端对话循环。

    Args:
        provider: 模型服务适配器。
        registry: 可选工具注册中心；省略时启用项目内置的两个教学工具。
        input_func: 终端输入函数，测试时可注入假输入。
        output_func: 终端输出函数，测试时可注入列表的 ``append``。

    CLI 只管理输入输出，模型与工具之间的多步编排由 ``ToolCallingAgent`` 负责。
    """

    # 显式判断 None，使调用者未来即使传入“空注册中心”也能表达禁用全部工具的意图。
    active_registry = registry if registry is not None else create_default_tool_registry()
    agent = ToolCallingAgent(provider, active_registry, DEFAULT_SYSTEM_PROMPT)
    output_func("FinAgent 对话已启动。输入 exit、quit 或 退出 可结束会话。")
    try:
        while True:
            try:
                user_input = input_func("你：")
            except (EOFError, KeyboardInterrupt):
                # Ctrl+D/Ctrl+Z 或 Ctrl+C 都属于正常终止方式，不应显示整段异常栈。
                output_func("\n会话已结束。")
                break

            if user_input.strip().lower() in EXIT_COMMANDS:
                output_func("会话已结束。")
                break
            if not user_input.strip():
                output_func("请输入内容后再发送。")
                continue

            try:
                answer = await agent.ask(user_input)
            except ModelProviderError as error:
                # 单轮失败不终止整个会话，用户修复网络或稍后即可继续重试。
                output_func(format_provider_error(error))
                continue
            except AgentError as error:
                # 达到工具循环上限等编排问题只影响本轮，正式历史不会被污染。
                output_func(f"Agent 执行失败：{error}")
                continue
            output_func(f"FinAgent：{answer}")
    finally:
        # 无论正常退出还是出现意外异常，都必须关闭底层 HTTP 客户端。
        await agent.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    dashboard_runner: DashboardRunner = run_dashboard_server,
) -> None:
    """解析命令并启动对应功能；这是 pyproject.toml 注册的 CLI 入口。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in {"chat", "dashboard"}:
        parser.print_help()
        return

    try:
        settings = get_settings()
    except ValidationError:
        # Pydantic 的完整错误适合开发调试，但 CLI 用户更需要明确的修复动作。
        print("配置加载失败，请检查项目根目录 .env 中的环境变量。")
        return

    if args.command == "dashboard":
        if args.host not in LOCAL_HOSTS:
            print(
                "警告：资产面板没有登录认证，当前监听地址会向局域网开放读写接口。"
                "请只在可信网络中使用。"
            )
        print(f"FinAgent 资产面板 API：http://{args.host}:{args.port}/api/v1/health")
        dashboard_runner(args.host, args.port)
        return

    try:
        provider = BailianModelProvider(settings)
    except ValueError as error:
        print(f"模型配置失败：{error}")
        return
    asyncio.run(run_chat(provider))


if __name__ == "__main__":
    main()
