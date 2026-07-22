"""阿里云百炼 OpenAI 兼容接口适配器。

本模块只负责两件事：把项目统一请求翻译成百炼请求，以及把百炼响应还原成统一
响应。Agent 的业务流程不应直接导入 OpenAI SDK，这个边界使未来接入 Ollama 等
本地模型时无需改写 Agent 循环。
"""

import json
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from finagent.core.config import Settings
from finagent.llm.errors import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelProviderError,
    ModelRateLimitError,
    ModelResponseError,
    ModelTimeoutError,
)
from finagent.llm.schemas import (
    FinishReason,
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)


class BailianModelProvider:
    """通过 OpenAI 兼容协议访问阿里云百炼千问模型。

    Args:
        settings: 已校验的模型配置。
        client: 可选的 SDK 客户端，主要用于测试和依赖注入。省略时由 Provider 创建。

    只有 Provider 自己创建的客户端才会在 ``close`` 中关闭，避免误关外部共享连接。
    """

    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        # Settings 允许离线 Dashboard 缺少模型密钥；模型 Provider 是真正需要密钥的边界，
        # 即使测试注入客户端也必须保持同一配置契约。
        api_key = settings.require_llm_api_key()
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=str(settings.llm_base_url),
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """发送一次模型请求并返回与厂商无关的标准响应。

        Raises:
            ModelAuthenticationError: 密钥或模型权限无效。
            ModelRateLimitError: 请求频率或配额受到限制。
            ModelTimeoutError: 请求超时。
            ModelProviderError: 其他百炼 API 错误。
            ModelResponseError: 响应结构或工具参数不合法。
        """

        # kwargs 的形状会随是否启用工具而变化，因此在 SDK 边界使用 Any；进入项目
        # 领域层后立即转换为强类型 ModelResponse，不让松散的外部类型继续传播。
        request_kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [self._message_to_api(message) for message in request.messages],
            "temperature": request.temperature
            if request.temperature is not None
            else self._settings.llm_temperature,
            "max_tokens": request.max_output_tokens or self._settings.llm_max_output_tokens,
            "extra_body": {
                "enable_thinking": request.enable_thinking
                if request.enable_thinking is not None
                else self._settings.llm_enable_thinking
            },
        }
        if request.tools:
            request_kwargs["tools"] = [self._tool_to_api(tool) for tool in request.tools]
            request_kwargs["tool_choice"] = request.tool_choice

        try:
            response = await self._client.chat.completions.create(**request_kwargs)
        except AuthenticationError as exc:
            raise ModelAuthenticationError("百炼鉴权失败，请检查 API Key 和模型权限") from exc
        except RateLimitError as exc:
            raise ModelRateLimitError("百炼请求受到限流或账户配额不足") from exc
        except APITimeoutError as exc:
            raise ModelTimeoutError("百炼模型请求超时") from exc
        except APIConnectionError as exc:
            raise ModelConnectionError("无法连接百炼，请检查网络、代理和 TLS 设置") from exc
        except APIError as exc:
            raise ModelProviderError(f"百炼 API 请求失败：{exc}") from exc

        return self._response_from_api(response)

    async def close(self) -> None:
        """关闭由本 Provider 创建的 HTTP 客户端。"""

        if self._owns_client:
            await self._client.close()

    @staticmethod
    def _message_to_api(message: Message) -> dict[str, Any]:
        """把一条统一消息转换为 Chat Completions 消息字典。"""

        result: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.role is MessageRole.ASSISTANT and message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        if message.role is MessageRole.TOOL:
            result["tool_call_id"] = message.tool_call_id
        return result

    @staticmethod
    def _tool_to_api(tool: ToolDefinition) -> dict[str, Any]:
        """把项目工具定义转换为 OpenAI Function Calling 格式。"""

        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": tool.strict,
            },
        }

    @staticmethod
    def _response_from_api(response: Any) -> ModelResponse:
        """校验并标准化百炼响应，隔离外部 SDK 数据结构。"""

        if not response.choices:
            raise ModelResponseError("百炼响应中没有 choices")

        choice = response.choices[0]
        parsed_calls: list[ToolCall] = []
        for api_call in choice.message.tool_calls or []:
            try:
                arguments = json.loads(api_call.function.arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ModelResponseError("模型返回的工具参数不是合法 JSON") from exc
            if not isinstance(arguments, dict):
                raise ModelResponseError("模型返回的工具参数 JSON 根节点必须是 object")
            parsed_calls.append(
                ToolCall(id=api_call.id, name=api_call.function.name, arguments=arguments)
            )

        usage = response.usage
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        reason = getattr(choice, "finish_reason", None)
        try:
            # 外部 SDK 字段在类型层面可能不是字符串；显式校验后再进入领域枚举。
            finish_reason = (
                FinishReason(reason) if isinstance(reason, str) else FinishReason.UNKNOWN
            )
        except ValueError:
            finish_reason = FinishReason.UNKNOWN

        return ModelResponse(
            model=response.model,
            content=choice.message.content,
            tool_calls=tuple(parsed_calls),
            finish_reason=finish_reason,
            usage=TokenUsage(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
                cached_input_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
                reasoning_output_tokens=getattr(completion_details, "reasoning_tokens", 0) or 0,
            ),
            response_id=getattr(response, "id", None),
        )
