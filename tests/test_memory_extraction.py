"""模型候选记忆抽取器的纯离线协议测试。

测试用顺序 Fake Provider 代替百炼，确保抽取请求禁用工具、只接受严格 JSON，并且模型不能
伪造来源 UUID 或 ACTIVE 状态。这里不访问数据库；来源与敏感信息的第二层校验由
``MemoryService`` 的既有测试覆盖。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from finagent.llm import (
    MessageRole,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from finagent.memory import (
    ConversationMessage,
    MemoryExtractionError,
    MemoryScopeType,
    MemoryType,
    ModelMemoryCandidateExtractor,
)

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


class SequenceProvider:
    """返回预设响应并记录抽取器发出的统一模型请求。"""

    def __init__(self, response: ModelResponse) -> None:
        self._response = response
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """记录请求并返回固定响应。"""

        self.requests.append(request)
        return self._response

    async def close(self) -> None:
        """Fake Provider 没有需要释放的网络资源。"""


def _user_message() -> ConversationMessage:
    """构造一条已经由会话服务持久化的 user 来源消息。"""

    return ConversationMessage(
        role=MessageRole.USER,
        content="以后分析 017811 时优先说明数据日期",
        session_id=uuid4(),
        sequence_number=1,
        created_at=NOW,
    )


async def test_model_extractor_returns_valid_draft_without_trusted_fields() -> None:
    """合法 JSON 应变成草稿，且请求必须禁用工具并关闭随机采样。"""

    provider = SequenceProvider(
        ModelResponse(
            model="fake-model",
            content=(
                '{"candidate":{"memory_type":"preference",'
                '"memory_key":"report.data_date",'
                '"value":{"priority":"high"},"scope_type":"asset",'
                '"scope_id":"017811","ttl_seconds":null}}'
            ),
        )
    )
    extractor = ModelMemoryCandidateExtractor(provider)

    draft = await extractor.extract(_user_message(), "好的，后续会说明数据日期。")

    assert draft is not None
    assert draft.memory_type is MemoryType.PREFERENCE
    assert draft.scope_type is MemoryScopeType.ASSET
    assert draft.scope_id == "017811"
    assert not hasattr(draft, "status")
    assert not hasattr(draft, "source_session_id")
    request = provider.requests[0]
    assert request.tool_choice == "none"
    assert request.tools == ()
    assert request.temperature == 0


async def test_model_extractor_allows_explicit_no_candidate() -> None:
    """普通即时问题不应被强行写成长期记忆候选。"""

    provider = SequenceProvider(
        ModelResponse(model="fake-model", content='{"candidate":null}')
    )

    assert (
        await ModelMemoryCandidateExtractor(provider).extract(
            _user_message(),
            "这是当前组合快照。",
        )
        is None
    )


@pytest.mark.parametrize(
    "content",
    [
        "不是 JSON",
        '{"candidate":{"memory_type":"preference","status":"active"}}',
        '[{"candidate":null}]',
    ],
)
async def test_model_extractor_rejects_invalid_or_privileged_json(content: str) -> None:
    """非法根结构、缺字段或越权状态字段都必须在写库前失败。"""

    provider = SequenceProvider(ModelResponse(model="fake-model", content=content))

    with pytest.raises(MemoryExtractionError, match="结构化协议"):
        await ModelMemoryCandidateExtractor(provider).extract(
            _user_message(),
            "测试回答",
        )


async def test_model_extractor_rejects_tool_calls() -> None:
    """候选抽取不允许模型借工具调用触发任何外部动作。"""

    provider = SequenceProvider(
        ModelResponse(
            model="fake-model",
            tool_calls=(ToolCall(id="call-1", name="unexpected_tool"),),
        )
    )

    with pytest.raises(MemoryExtractionError, match="不允许返回工具调用"):
        await ModelMemoryCandidateExtractor(provider).extract(
            _user_message(),
            "测试回答",
        )
