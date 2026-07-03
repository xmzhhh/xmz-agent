"""大语言模型领域层的公共接口。

本包定义 FinAgent 自己的模型请求、响应和 Provider 协议。业务层只能依赖这里的
类型，不能直接依赖 OpenAI、百炼或 Ollama SDK 的返回对象。这样切换模型服务时，
只需新增或替换 Provider 适配器，不必修改 Agent 的核心流程。
"""

from finagent.llm.base import ModelProvider
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

__all__ = [
    "FinishReason",
    "Message",
    "MessageRole",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "TokenUsage",
    "ToolCall",
    "ToolDefinition",
]
