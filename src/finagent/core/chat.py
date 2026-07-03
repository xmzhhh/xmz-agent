"""最小多轮对话会话。

本模块只管理“对话历史如何进入模型、成功回答后如何更新历史”，不负责读取终端输入
或打印文字。把会话逻辑与 CLI 分离后，未来的 Web API、桌面界面也能复用同一个会话
对象；后续加入上下文裁剪、长期记忆和 RAG 时，也有明确的扩展位置。
"""

from finagent.llm import Message, MessageRole, ModelProvider, ModelRequest, ModelResponseError


class ChatSession:
    """保存一次进程内多轮对话，并通过统一 Provider 获取回答。

    Args:
        provider: 实现统一模型协议的服务适配器。
        system_prompt: 约束模型身份与行为的系统提示词。

    当前版本只把历史保存在内存中，退出程序后会丢失。持久化记忆将在后续阶段实现。
    """

    def __init__(self, provider: ModelProvider, system_prompt: str) -> None:
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        self._provider = provider
        self._messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=system_prompt.strip())
        ]

    @property
    def messages(self) -> tuple[Message, ...]:
        """返回只读形式的当前对话历史，便于测试和未来的上下文管理。"""

        return tuple(self._messages)

    async def ask(self, user_input: str) -> str:
        """发送一轮用户消息，并在成功后把双方消息加入历史。

        Args:
            user_input: 用户本轮输入的自然语言文本。

        Returns:
            模型生成的非空文本回答。

        Raises:
            ValueError: 用户输入为空。
            ModelResponseError: 模型只返回工具调用而没有可展示文本。
            ModelProviderError: Provider 调用失败时透传其统一异常。

        历史只在模型成功返回文本后更新。这样网络失败后用户可以重试，而不会把一条
        没有对应回答的消息留在上下文中，造成对话状态不完整。
        """

        normalized_input = user_input.strip()
        if not normalized_input:
            raise ValueError("用户输入不能为空")

        user_message = Message(role=MessageRole.USER, content=normalized_input)
        response = await self._provider.generate(
            ModelRequest(messages=(*self._messages, user_message))
        )
        if response.content is None or not response.content.strip():
            raise ModelResponseError("当前 CLI 尚不能执行模型返回的工具调用")

        answer = response.content.strip()
        self._messages.extend([user_message, Message(role=MessageRole.ASSISTANT, content=answer)])
        return answer

    async def close(self) -> None:
        """释放会话所使用的模型客户端资源。"""

        await self._provider.close()
