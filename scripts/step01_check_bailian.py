"""Step 01：手动验证本地配置能否成功调用阿里云百炼。

这是面向学习和联调的可执行脚本，不替代 tests 中的自动化测试。它会产生一次真实
网络请求和少量 token 费用，因此不应加入日常单元测试，也绝不能打印 API Key。
"""

import argparse
import asyncio

from finagent.core.config import get_settings
from finagent.llm import BailianModelProvider, Message, MessageRole, ModelRequest


async def ask_bailian(prompt: str) -> None:
    """向当前配置的千问模型发送一句话并打印标准化结果。

    Args:
        prompt: 用户希望模型回答的问题。
    """

    provider = BailianModelProvider(get_settings())
    try:
        response = await provider.generate(
            ModelRequest(
                messages=(Message(role=MessageRole.USER, content=prompt),),
                # 连通性检查只需要短回答，限制输出可以减少等待时间和 token 成本。
                max_output_tokens=128,
            )
        )
        print(f"模型：{response.model}")
        print(f"回答：{response.content}")
        print(f"Token：输入 {response.usage.input_tokens}，输出 {response.usage.output_tokens}")
    finally:
        # 即使模型调用失败，也要释放底层 HTTP 连接。
        await provider.close()


def main() -> None:
    """解析命令行参数并启动异步模型调用。"""

    parser = argparse.ArgumentParser(description="验证 FinAgent 的百炼模型配置")
    parser.add_argument("prompt", nargs="?", default="请用一句话介绍你自己。")
    args = parser.parse_args()
    asyncio.run(ask_bailian(args.prompt))


if __name__ == "__main__":
    main()
