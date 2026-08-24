"""长期结构化记忆的确定性状态机与安全边界。

MemoryService 不调用大模型。模型只能提交候选命令；本服务验证来源和敏感字段，并且只有
``confirm_candidate`` 能把候选变为 ACTIVE。确认、替换、过期和删除都通过同一个 Unit of Work
同时修改正文与审计事件，避免数据库出现部分成功状态。
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import JsonValue

from finagent.llm import MessageRole
from finagent.memory.commands import (
    MemoryCandidateCreate,
    MemoryConfirmationResult,
    MemoryRejectionReason,
)
from finagent.memory.errors import (
    ConversationMessageNotFoundError,
    InvalidMemoryTransitionError,
    MemoryAuditError,
    MemoryCandidateExpiredError,
    MemoryClockError,
    MemorySourceError,
    SensitiveMemoryError,
)
from finagent.memory.models import (
    MemoryActor,
    MemoryEvent,
    MemoryEventType,
    MemoryItem,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
)
from finagent.memory.repository import MemoryRepository
from finagent.memory.unit_of_work import MemoryUnitOfWorkFactory

type Clock = Callable[[], datetime]

# 字段名先删除大小写和分隔符再比较，因此 ``openai_api_key``、``apiKey`` 和 ``API-KEY``
# 都会命中同一规则。列表只覆盖明确凭据与身份字段，普通投资偏好不会被误判为账号秘密。
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "apikey",
        "accesstoken",
        "refreshtoken",
        "password",
        "passwd",
        "secret",
        "credential",
        "privatekey",
        "mnemonic",
        "seedphrase",
        "bankcard",
        "cardnumber",
        "accountnumber",
        "idcard",
        "密码",
        "密钥",
        "令牌",
        "助记词",
        "银行卡",
        "身份证",
    }
)

# value 中不应出现自由文本凭据。这里识别标签而非具体值，异常也永远不回显原文。
_SENSITIVE_TEXT_MARKERS = (
    "api key",
    "access token",
    "refresh token",
    "bearer ",
    "password",
    "private key",
    "seed phrase",
    "密码",
    "密钥",
    "令牌",
    "助记词",
    "银行卡",
    "身份证",
)

# 审计详情只允许不含正文的版本号、状态、布尔标记、原因代码和关联 UUID。
_AUDIT_DETAIL_KEYS: dict[MemoryEventType, frozenset[str]] = {
    MemoryEventType.CANDIDATE_CREATED: frozenset({"version", "has_expiry"}),
    MemoryEventType.CONFIRMED: frozenset({"version", "superseded_memory_id"}),
    MemoryEventType.REJECTED: frozenset({"version", "reason_code"}),
    MemoryEventType.SUPERSEDED: frozenset({"version", "replacement_memory_id"}),
    MemoryEventType.EXPIRED: frozenset({"version"}),
    MemoryEventType.DELETED: frozenset({"version", "prior_status", "reason_code"}),
}


class MemoryService:
    """管理长期记忆候选、人工确认、版本冲突和生命周期。

    Args:
        unit_of_work_factory: 为每次状态迁移提供会话、记忆和审计的原子事务。
        clock: 生成服务端时间；测试可注入可控时钟，生产默认使用 UTC。
    """

    def __init__(
        self,
        unit_of_work_factory: MemoryUnitOfWorkFactory,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock

    async def create_candidate(self, command: MemoryCandidateCreate) -> MemoryItem:
        """验证模型建议并保存 CANDIDATE 记忆和创建事件。

        候选必须追溯到真实 user 消息，且结构化键和值不能包含凭据字段或明显凭据标签。
        本方法没有任何参数允许调用方指定 ACTIVE 状态。
        """

        now = self._current_time()
        self._reject_sensitive_content(command.memory_key, command.value)
        expires_at = (
            now + timedelta(seconds=command.ttl_seconds)
            if command.ttl_seconds is not None
            else None
        )

        async with self._unit_of_work_factory() as unit_of_work:
            try:
                source_message = await unit_of_work.conversations.get_message(
                    command.source_message_id
                )
            except ConversationMessageNotFoundError as error:
                raise MemorySourceError("候选记忆的来源消息不存在") from error
            if source_message.session_id != command.source_session_id:
                raise MemorySourceError("候选记忆的来源消息与会话不匹配")
            if source_message.role is not MessageRole.USER:
                raise MemorySourceError("长期记忆候选只能来源于 user 消息")

            memory = MemoryItem(
                memory_type=command.memory_type,
                memory_key=command.memory_key,
                value=command.value,
                scope_type=command.scope_type,
                scope_id=command.scope_id,
                source_session_id=command.source_session_id,
                source_message_id=command.source_message_id,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            await unit_of_work.memories.add_memory(memory)
            await unit_of_work.memories.add_event(
                self._build_event(
                    memory.id,
                    MemoryEventType.CANDIDATE_CREATED,
                    MemoryActor.MODEL,
                    {"version": memory.version, "has_expiry": expires_at is not None},
                    now,
                )
            )
            await unit_of_work.commit()
            return memory

    async def confirm_candidate(self, memory_id: UUID) -> MemoryConfirmationResult:
        """由用户确认候选；同身份旧 ACTIVE 记忆会被原子替换。

        新版本通过 ``supersedes_id`` 指向旧版本，版本号在旧版本基础上加一；旧正文继续保留为
        SUPERSEDED，便于用户查看历史。候选到期后不得通过确认重新激活。
        """

        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            candidate = await unit_of_work.memories.get_memory(memory_id)
            self._require_candidate(candidate, "确认")
            self._ensure_time_not_reversed(candidate, now)
            if candidate.expires_at is not None and candidate.expires_at <= now:
                raise MemoryCandidateExpiredError("候选记忆已经过期，不能确认")

            identity_matches = await unit_of_work.memories.list_memories(
                memory_type=candidate.memory_type,
                memory_key=candidate.memory_key,
                scope_type=candidate.scope_type,
                scope_id=candidate.scope_id,
            )
            active_matches = tuple(
                memory
                for memory in identity_matches
                if memory.status is MemoryStatus.ACTIVE
            )
            # 数据库部分唯一索引保证至多一条；显式检查让损坏数据不会被静默选取。
            if len(active_matches) > 1:
                raise InvalidMemoryTransitionError("同一记忆身份存在多条 ACTIVE 数据")
            previous_active = active_matches[0] if active_matches else None

            # ACTIVE 可能在后台扫描前已经到期。确认新候选时先补做过期迁移，不能把过期值继续
            # 当成正在生效的冲突；其历史版本号和 lineage 仍需保留。
            if (
                previous_active is not None
                and previous_active.expires_at is not None
                and previous_active.expires_at <= now
            ):
                await self._expire_items(
                    unit_of_work.memories,
                    (previous_active,),
                    now,
                )
                previous_active = None

            confirmed_history = tuple(
                memory
                for memory in identity_matches
                if memory.status
                in {
                    MemoryStatus.ACTIVE,
                    MemoryStatus.SUPERSEDED,
                    MemoryStatus.EXPIRED,
                }
            )
            latest_history = (
                max(
                    confirmed_history,
                    key=lambda memory: (
                        memory.version,
                        memory.created_at,
                        str(memory.id),
                    ),
                )
                if confirmed_history
                else None
            )
            if previous_active is not None and latest_history != previous_active:
                raise InvalidMemoryTransitionError("ACTIVE 记忆不是当前版本链的最新版本")

            version = latest_history.version + 1 if latest_history is not None else 1
            supersedes_id = latest_history.id if latest_history is not None else None

            if previous_active is not None:
                self._ensure_time_not_reversed(previous_active, now)
                superseded = MemoryItem.model_validate(
                    {
                        **previous_active.model_dump(),
                        "status": MemoryStatus.SUPERSEDED,
                        "updated_at": now,
                    }
                )
                await unit_of_work.memories.update_memory(superseded)
            else:
                superseded = None

            active = MemoryItem.model_validate(
                {
                    **candidate.model_dump(),
                    "status": MemoryStatus.ACTIVE,
                    "version": version,
                    "supersedes_id": supersedes_id,
                    "confirmed_at": now,
                    "updated_at": now,
                }
            )
            await unit_of_work.memories.update_memory(active)

            if superseded is not None:
                await unit_of_work.memories.add_event(
                    self._build_event(
                        superseded.id,
                        MemoryEventType.SUPERSEDED,
                        MemoryActor.USER,
                        {
                            "version": superseded.version,
                            "replacement_memory_id": str(active.id),
                        },
                        now,
                    )
                )
            confirmed_details: dict[str, JsonValue] = {"version": active.version}
            if supersedes_id is not None:
                confirmed_details["superseded_memory_id"] = str(supersedes_id)
            await unit_of_work.memories.add_event(
                self._build_event(
                    active.id,
                    MemoryEventType.CONFIRMED,
                    MemoryActor.USER,
                    confirmed_details,
                    now,
                )
            )
            await unit_of_work.commit()
            return MemoryConfirmationResult(
                memory=active,
                superseded_memory=superseded,
            )

    async def reject_candidate(
        self,
        memory_id: UUID,
        *,
        reason: MemoryRejectionReason = MemoryRejectionReason.USER_DECISION,
    ) -> MemoryItem:
        """由用户拒绝候选，并保存不含自由文本的原因代码。"""

        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            candidate = await unit_of_work.memories.get_memory(memory_id)
            self._require_candidate(candidate, "拒绝")
            self._ensure_time_not_reversed(candidate, now)
            rejected = MemoryItem.model_validate(
                {
                    **candidate.model_dump(),
                    "status": MemoryStatus.REJECTED,
                    "updated_at": now,
                }
            )
            await unit_of_work.memories.update_memory(rejected)
            await unit_of_work.memories.add_event(
                self._build_event(
                    rejected.id,
                    MemoryEventType.REJECTED,
                    MemoryActor.USER,
                    {"version": rejected.version, "reason_code": reason.value},
                    now,
                )
            )
            await unit_of_work.commit()
            return rejected

    async def list_active_memories(
        self,
        *,
        memory_type: MemoryType | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
    ) -> tuple[MemoryItem, ...]:
        """返回可以注入 Agent 上下文的 ACTIVE 且未过期记忆。

        查询过程会在同一事务内把到期数据转为 EXPIRED 并写审计，然后只返回剩余有效项。
        """

        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            active = await unit_of_work.memories.list_memories(
                status=MemoryStatus.ACTIVE,
                memory_type=memory_type,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            expired = await self._expire_items(unit_of_work.memories, active, now)
            await unit_of_work.commit()
            expired_ids = {memory.id for memory in expired}
            return tuple(memory for memory in active if memory.id not in expired_ids)

    async def expire_due_memories(self) -> tuple[MemoryItem, ...]:
        """显式扫描全部 ACTIVE 记忆，把已到期项目转为 EXPIRED。"""

        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            active = await unit_of_work.memories.list_memories(status=MemoryStatus.ACTIVE)
            expired = await self._expire_items(unit_of_work.memories, active, now)
            await unit_of_work.commit()
            return expired

    async def delete_memory(self, memory_id: UUID) -> MemoryItem:
        """按用户请求硬删除正文，同时保留最小删除审计。"""

        now = self._current_time()
        async with self._unit_of_work_factory() as unit_of_work:
            memory = await unit_of_work.memories.get_memory(memory_id)
            self._ensure_time_not_reversed(memory, now)
            await unit_of_work.memories.add_event(
                self._build_event(
                    memory.id,
                    MemoryEventType.DELETED,
                    MemoryActor.USER,
                    {
                        "version": memory.version,
                        "prior_status": memory.status.value,
                        "reason_code": "user_request",
                    },
                    now,
                )
            )
            await unit_of_work.memories.delete_memory(memory.id)
            await unit_of_work.commit()
            return memory

    async def _expire_items(
        self,
        repository: MemoryRepository,
        active_items: tuple[MemoryItem, ...],
        now: datetime,
    ) -> tuple[MemoryItem, ...]:
        """在调用方事务中更新到期 ACTIVE 记忆并追加系统事件。"""

        expired_items: list[MemoryItem] = []
        for memory in active_items:
            if memory.expires_at is None or memory.expires_at > now:
                continue
            self._ensure_time_not_reversed(memory, now)
            expired = MemoryItem.model_validate(
                {
                    **memory.model_dump(),
                    "status": MemoryStatus.EXPIRED,
                    "updated_at": now,
                }
            )
            await repository.update_memory(expired)
            await repository.add_event(
                self._build_event(
                    expired.id,
                    MemoryEventType.EXPIRED,
                    MemoryActor.SYSTEM,
                    {"version": expired.version},
                    now,
                )
            )
            expired_items.append(expired)
        return tuple(expired_items)

    @staticmethod
    def _require_candidate(memory: MemoryItem, action: str) -> None:
        """只允许 CANDIDATE 进入确认或拒绝迁移。"""

        if memory.status is not MemoryStatus.CANDIDATE:
            raise InvalidMemoryTransitionError(
                f"只有 candidate 状态可以{action}，当前状态为 {memory.status.value}"
            )

    @staticmethod
    def _ensure_time_not_reversed(memory: MemoryItem, now: datetime) -> None:
        """拒绝服务端时钟早于记录更新时间，避免审计时间线倒退。"""

        if now < memory.updated_at:
            raise MemoryClockError("服务端时钟早于记忆最近更新时间")

    def _current_time(self) -> datetime:
        """读取并校验服务端时钟必须包含时区。"""

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise MemoryClockError("MemoryService 的 clock 必须返回带时区时间")
        return now

    @staticmethod
    def _reject_sensitive_content(memory_key: str, value: dict[str, JsonValue]) -> None:
        """在创建 ORM 对象前拒绝凭据字段，异常消息绝不回显候选正文。"""

        if _sensitive_key(memory_key) or _contains_sensitive_content(value):
            raise SensitiveMemoryError("候选记忆包含禁止保存的敏感字段或凭据内容")

    @staticmethod
    def _build_event(
        memory_id: UUID,
        event_type: MemoryEventType,
        actor: MemoryActor,
        details: dict[str, JsonValue],
        occurred_at: datetime,
    ) -> MemoryEvent:
        """使用事件类型白名单校验审计字段，防止正文被顺手复制进 details。"""

        allowed_keys = _AUDIT_DETAIL_KEYS[event_type]
        unexpected_keys = set(details) - allowed_keys
        if unexpected_keys:
            raise MemoryAuditError("记忆审计详情包含未列入白名单的字段")
        return MemoryEvent(
            memory_id=memory_id,
            event_type=event_type,
            actor=actor,
            details=details,
            occurred_at=occurred_at,
        )


def _compact_key(value: str) -> str:
    """删除分隔符并统一大小写，减少敏感字段通过拼写变体绕过检查的机会。"""

    return "".join(character for character in value.casefold() if character.isalnum())


def _sensitive_key(value: str) -> bool:
    """判断字段名是否包含明确的凭据或身份标记。"""

    compact = _compact_key(value)
    return any(token in compact for token in _SENSITIVE_KEY_TOKENS)


def _contains_sensitive_content(value: object) -> bool:
    """递归扫描 JSON 键和字符串标签，不收集或回显命中的具体内容。"""

    if isinstance(value, dict):
        return any(
            _sensitive_key(str(key)) or _contains_sensitive_content(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_content(item) for item in value)
    if isinstance(value, str):
        normalized = value.casefold()
        return any(marker in normalized for marker in _SENSITIVE_TEXT_MARKERS)
    return False
