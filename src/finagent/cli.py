"""FinAgent 命令行入口。

CLI 只负责解析命令、接收用户输入和展示结果；多轮历史由 ``ChatSession`` 管理，模型
厂商差异由 Provider 管理。保持入口层轻量，可以避免界面代码与核心业务相互耦合。
"""

import argparse
import asyncio
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from finagent.core.chat import ChatSession
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

DEFAULT_SYSTEM_PROMPT = (
    "你是 FinAgent，一名谨慎、客观的 AI 投资研究助手。"
    "你需要区分事实、推测和观点，不承诺收益，也不替用户做最终投资决定。"
)
EXIT_COMMANDS = {"exit", "quit", "退出"}


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器，集中维护所有子命令及帮助文本。"""

    parser = argparse.ArgumentParser(prog="finagent", description="FinAgent AI 投资研究助手")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("chat", help="启动与千问模型的多轮对话")
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
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> None:
    """运行多轮终端对话循环。

    输入和输出函数允许在测试中注入假终端，避免测试真的等待键盘输入。生产环境使用
    默认的 ``input`` 和 ``print``，这是一种简单的依赖注入。
    """

    session = ChatSession(provider, DEFAULT_SYSTEM_PROMPT)
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
                answer = await session.ask(user_input)
            except ModelProviderError as error:
                # 单轮失败不终止整个会话，用户修复网络或稍后即可继续重试。
                output_func(format_provider_error(error))
                continue
            output_func(f"FinAgent：{answer}")
    finally:
        # 无论正常退出还是出现意外异常，都必须关闭底层 HTTP 客户端。
        await session.close()


def main(argv: Sequence[str] | None = None) -> None:
    """解析命令并启动对应功能；这是 pyproject.toml 注册的 CLI 入口。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "chat":
        parser.print_help()
        return

    try:
        settings = get_settings()
    except ValidationError:
        # Pydantic 的完整错误适合开发调试，但 CLI 用户更需要明确的修复动作。
        print("配置加载失败，请检查项目根目录 .env 中的 LLM_API_KEY 等配置。")
        return

    asyncio.run(run_chat(BailianModelProvider(settings)))


if __name__ == "__main__":
    main()
