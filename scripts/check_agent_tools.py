"""使用真实百炼模型检查 Agent 工具调用闭环。

这是供学习者在 PyCharm 中直接运行的人工验收脚本，不属于 pytest 自动测试。它会访问
真实百炼接口并消耗少量 token，因此不应放进自动化测试。脚本要求模型使用确定性的
仓位计算工具，并打印实际调用记录，避免只看到正确答案却无法确认工具是否真的执行。
"""

import asyncio

from finagent.agents import ToolCallingAgent
from finagent.cli import DEFAULT_SYSTEM_PROMPT
from finagent.core.config import get_settings
from finagent.llm import BailianModelProvider
from finagent.tools import create_default_tool_registry


async def check_agent_tools() -> None:
    """发起一次真实工具调用，并展示最终回答和工具调用轨迹。"""

    provider = BailianModelProvider(get_settings())
    agent = ToolCallingAgent(
        provider,
        create_default_tool_registry(),
        DEFAULT_SYSTEM_PROMPT,
    )

    try:
        answer = await agent.ask(
            "请务必使用工具计算：3000 元黄金持仓占 10000 元总资产的比例是多少？"
            "请说明你使用了什么工具。"
        )
        called_tools = [call.name for message in agent.messages for call in message.tool_calls]

        print(f"最终回答：{answer}")
        print(f"实际工具调用：{called_tools or '模型没有调用工具'}")
    finally:
        # 真实 Provider 持有 HTTP 连接，即使调用失败也必须释放。
        await agent.close()


def main() -> None:
    """同步脚本入口，由 asyncio 负责创建和关闭事件循环。"""

    asyncio.run(check_agent_tools())


if __name__ == "__main__":
    main()
