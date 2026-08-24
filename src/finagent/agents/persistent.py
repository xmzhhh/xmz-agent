"""把持久化上下文、模型工具循环和整轮事务提交组合成可恢复 Agent。

本模块位于 Agent 编排层：它不关心 SQLite、百炼或具体投资工具的实现，只依赖
``ContextAssembler``、``ModelProvider``、``ToolRegistry`` 和 ``ConversationService``
这四个稳定接口。模型推理和工具执行全部先发生在内存中，只有最终回答成功生成后，
本轮完整消息才会以一笔短事务写入会话历史。
"""

import asyncio
import json
from collections.abc import Iterable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from finagent.agents.errors import AgentResponseError, AgentStepLimitError
from finagent.llm import Message, MessageRole, ModelProvider, ModelRequest, ToolCall
from finagent.memory.context import ContextAssembler
from finagent.memory.conversation import ConversationService
from finagent.memory.models import ConversationMessage
from finagent.tools import ToolError, ToolRegistry


class PersistentAgentTurnResult(BaseModel):
    """一次成功持久化的 Agent 对话结果。

    Attributes:
        session_id: 对话所属会话 ID。
        answer: 可直接展示给用户的最终回答。
        messages: 本轮成功写入 SQLite 的完整消息，包含工具协议消息。
        model_call_count: 本轮主 Agent 调用模型的次数，不包含滚动摘要器调用。
        tool_call_names: 按实际执行顺序记录的工具名称；工具参数失败也算一次执行尝试。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    answer: str = Field(min_length=1)
    messages: tuple[ConversationMessage, ...] = Field(min_length=2)
    model_call_count: int = Field(ge=1)
    tool_call_names: tuple[str, ...] = ()


class PersistentToolCallingAgent:
    """使用 SQLite 会话上下文执行一轮可回滚的模型工具循环。

    Args:
        provider: 与模型厂商无关的统一模型接口。
        registry: 本 Agent 获准调用的工具白名单。本阶段只注入无副作用的教学工具，
            下一小阶段会加入 Phase 7 SQLite 的只读资产工具。
        context_assembler: 负责装配系统提示、已确认长期记忆、摘要和近期消息。
        conversation_service: 负责把成功轮次原子写入持久化会话。
        system_prompt: Agent 的身份、安全边界和工具使用规则。
        max_steps: 一轮用户输入允许的最大模型调用次数，防止无限工具循环。

    同一进程内，同一个会话的请求由会话级锁串行执行，使后一个请求一定能读取前一个
    请求刚提交的上下文；不同会话仍可并发。数据库提交时还会比较会话更新时间，作为
    多进程并发的乐观锁：如果推理期间历史被其他进程改变，本轮结果会被拒绝而非覆盖。
    """

    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        context_assembler: ContextAssembler,
        conversation_service: ConversationService,
        system_prompt: str,
        *,
        max_steps: int = 5,
    ) -> None:
        normalized_prompt = system_prompt.strip()
        if not normalized_prompt:
            raise ValueError("system_prompt 不能为空")
        if max_steps < 1:
            raise ValueError("max_steps 必须大于等于 1")

        self._provider = provider
        self._registry = registry
        self._context_assembler = context_assembler
        self._conversation_service = conversation_service
        self._system_prompt = normalized_prompt
        self._max_steps = max_steps
        self._session_locks: dict[UUID, asyncio.Lock] = {}

    async def ask(
        self,
        session_id: UUID,
        user_input: str,
        *,
        asset_symbols: Iterable[str] = (),
    ) -> PersistentAgentTurnResult:
        """读取持久化上下文，完成一轮工具调用，并原子保存成功结果。

        Args:
            session_id: 已存在且未归档的会话 ID。
            user_input: 用户本轮自然语言输入，首尾空白会被去除。
            asset_symbols: 本轮涉及的资产代码，只用于选择对应资产范围的长期记忆。

        Returns:
            最终回答、本轮持久化消息以及模型和工具调用轨迹摘要。

        Raises:
            ValueError: 用户输入为空。
            AgentStepLimitError: 达到模型调用上限后仍请求工具。
            AgentResponseError: 模型没有生成可展示的最终文本。
            ModelProviderError: 任意模型请求失败。
            ConversationConflictError: 推理期间同一会话被其他请求更新。

        除成功返回外，本方法不会调用 ``commit_turn``，因此网络失败、步数超限等情况
        都不会在数据库中留下半轮消息。工具参数错误会先作为结构化 tool 消息交还模型；
        如果模型随后正常回答，这段纠错轨迹会与最终回答一起保存，便于恢复与审计。
        """

        normalized_input = user_input.strip()
        if not normalized_input:
            raise ValueError("用户输入不能为空")

        # setdefault 在事件循环交出控制权之前同步完成，因此同一 Agent 实例不会为同一
        # session_id 创建两个不同的锁。锁覆盖上下文读取、推理和最终短事务提交。
        session_lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with session_lock:
            return await self._ask_locked(
                session_id,
                normalized_input,
                asset_symbols=asset_symbols,
            )

    async def _ask_locked(
        self,
        session_id: UUID,
        normalized_input: str,
        *,
        asset_symbols: Iterable[str],
    ) -> PersistentAgentTurnResult:
        """在会话锁内推进上下文、工具循环和事务提交。"""

        context = await self._context_assembler.assemble(
            session_id,
            system_prompt=self._system_prompt,
            asset_symbols=asset_symbols,
        )
        user_message = Message(role=MessageRole.USER, content=normalized_input)

        # working_messages 是发送给模型的完整上下文；turn_messages 只保存本轮增量，
        # 最终写库时不能把 system prompt、摘要或旧消息重复保存。
        working_messages = [*context.messages, user_message]
        turn_messages: list[Message] = [user_message]
        executed_tool_names: list[str] = []

        for step in range(1, self._max_steps + 1):
            response = await self._provider.generate(
                ModelRequest(
                    messages=tuple(working_messages),
                    tools=self._registry.definitions,
                    tool_choice="auto",
                )
            )
            assistant_message = Message(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            )
            working_messages.append(assistant_message)
            turn_messages.append(assistant_message)

            if not response.tool_calls:
                if response.content is None or not response.content.strip():
                    raise AgentResponseError("模型没有返回最终文本回答")

                answer = response.content.strip()
                persisted = await self._conversation_service.commit_turn(
                    session_id,
                    tuple(turn_messages),
                    expected_session_updated_at=context.session.updated_at,
                )
                return PersistentAgentTurnResult(
                    session_id=session_id,
                    answer=answer,
                    messages=persisted,
                    model_call_count=step,
                    tool_call_names=tuple(executed_tool_names),
                )

            if step == self._max_steps:
                # 已经没有下一次模型调用来读取工具结果，所以不执行最后一步请求的工具。
                # 这既节省开销，也避免未来误执行可能带副作用的工具。
                raise AgentStepLimitError(
                    f"Agent 在 {self._max_steps} 次模型调用内没有生成最终回答"
                )

            for tool_call in response.tool_calls:
                tool_message = await self._execute_tool_call(tool_call)
                executed_tool_names.append(tool_call.name)
                working_messages.append(tool_message)
                turn_messages.append(tool_message)

        raise AgentStepLimitError("Agent 工具调用循环意外结束")

    async def _execute_tool_call(self, tool_call: ToolCall) -> Message:
        """执行工具，并把成功结果或可纠正错误转换成标准 tool 消息。"""

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
        """释放 Agent 使用的模型 Provider 网络资源。"""

        await self._provider.close()
