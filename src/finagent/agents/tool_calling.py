"""最小可控的 Agent 工具调用循环。

本模块负责把模型、对话历史和工具注册中心组织成一个完整循环：模型先决定是否调用
工具，应用校验并执行工具，再把结果作为 tool 消息交还模型，直到得到最终文本回答。
终端输入输出不属于这里，因此未来 Web API 也能直接复用这个 Agent。
"""

import json

from finagent.agents.errors import AgentResponseError, AgentStepLimitError
from finagent.llm import (
    Message,
    MessageRole,
    ModelProvider,
    ModelRequest,
    ToolCall,
)
from finagent.tools import ToolError, ToolRegistry


class ToolCallingAgent:
    """维护多轮历史并执行“模型—工具—模型”循环。

    Args:
        provider: 与具体模型厂商无关的模型调用接口。
        registry: 本 Agent 被允许使用的工具白名单。
        system_prompt: 定义 Agent 身份、边界和工具使用原则的系统提示词。
        max_steps: 一次用户提问允许发起的最大模型调用次数，必须大于等于 1。

    ``max_steps`` 是防止模型不断请求工具而形成无限循环的安全阀。最后一步如果仍返回
    工具调用，Agent 会在执行工具前停止，因为已经没有下一次模型调用来消费工具结果；
    这也避免无意义地执行未来可能带副作用的工具。
    """

    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        system_prompt: str,
        *,
        max_steps: int = 5,
    ) -> None:
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        if max_steps < 1:
            raise ValueError("max_steps 必须大于等于 1")

        self._provider = provider
        self._registry = registry
        self._max_steps = max_steps
        self._messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=system_prompt.strip())
        ]

    @property
    def messages(self) -> tuple[Message, ...]:
        """返回只读形式的完整历史，包括 assistant 工具请求和 tool 执行结果。"""

        return tuple(self._messages)

    async def ask(self, user_input: str) -> str:
        """处理一轮用户提问，必要时多次调用模型和工具。

        Args:
            user_input: 用户本轮输入的自然语言文本。

        Returns:
            模型读取工具结果后生成的最终非空文本。

        Raises:
            ValueError: 用户输入为空。
            AgentStepLimitError: 达到最大模型调用次数后仍在请求工具。
            AgentResponseError: 模型没有返回可展示文本。
            ModelProviderError: 任意一次模型调用失败时透传模型层统一异常。

        本轮先在 ``working_messages`` 副本中推进，只有成功获得最终回答才整体提交到正式
        历史。这样网络失败或步数超限后，下一轮不会携带半截 assistant/tool 消息。
        """

        normalized_input = user_input.strip()
        if not normalized_input:
            raise ValueError("用户输入不能为空")

        user_message = Message(role=MessageRole.USER, content=normalized_input)
        working_messages = [*self._messages, user_message]

        for step in range(1, self._max_steps + 1):
            response = await self._provider.generate(
                ModelRequest(
                    messages=tuple(working_messages),
                    tools=self._registry.definitions,
                    tool_choice="auto",
                )
            )

            # 必须先保存模型的工具调用消息。之后的 tool 消息通过 tool_call_id 与这里的
            # 调用一一对应，这是 OpenAI 兼容 Function Calling 协议的必要顺序。
            assistant_message = Message(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            )
            working_messages.append(assistant_message)

            if not response.tool_calls:
                if response.content is None or not response.content.strip():
                    # ModelResponse 本身已校验该状态，保留此判断作为编排层防御式边界。
                    raise AgentResponseError("模型没有返回最终文本回答")

                answer = response.content.strip()
                # 只有完整成功的一轮才替换正式历史，保证会话状态始终可继续使用。
                self._messages = working_messages
                return answer

            if step == self._max_steps:
                raise AgentStepLimitError(
                    f"Agent 在 {self._max_steps} 次模型调用内没有生成最终回答"
                )

            for tool_call in response.tool_calls:
                tool_message = await self._execute_tool_call(tool_call)
                working_messages.append(tool_message)

        # range 理论上一定通过 return 或步数异常结束，此处用于帮助类型检查器理解边界。
        raise AgentStepLimitError("Agent 工具调用循环意外结束")

    async def _execute_tool_call(self, tool_call: ToolCall) -> Message:
        """执行一次工具调用，并把成功或失败都转换成标准 tool 消息。

        参数错误或未知工具不会直接终止 Agent，而是作为结构化错误返回模型。模型因此
        有机会修正参数、改用其他工具，或向用户解释无法完成请求。真正是否再次尝试仍
        受到 ``max_steps`` 限制。
        """

        try:
            result = await self._registry.execute(tool_call)
            content = result.to_message_content()
        except ToolError as error:
            content = json.dumps(
                {
                    "ok": False,
                    "tool_name": tool_call.name,
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        return Message(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=tool_call.id,
        )

    async def close(self) -> None:
        """释放 Agent 所使用的模型客户端资源。"""

        await self._provider.close()
