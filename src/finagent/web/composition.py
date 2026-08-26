"""根据应用配置装配 Dashboard、账本、记忆与 Agent 的正式运行时依赖。

组合根是唯一知道具体 Provider、SQLite Unit of Work 和服务依赖关系的位置。FastAPI 路由
只面向已经组装好的应用服务，因此 Fake/Real 行情切换、模型厂商替换或数据库实现变化都不
需要散落修改 HTTP 接口。资产服务与记忆服务共享一个 ``DatabaseManager``，但各自保留独立
事务工厂，避免把不相关 Repository 混进同一业务边界。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from finagent.agents import AgentApplicationService, PersistentToolCallingAgent
from finagent.core.config import Settings
from finagent.dashboard import (
    InMemoryDashboardUnitOfWorkFactory,
    InMemoryManualPriceRepository,
    PortfolioDashboardService,
)
from finagent.data import (
    AkShareFundNavProvider,
    FakeMarketDataProvider,
    GoldApiMarketDataProvider,
    MarketDataProvider,
    MarketDataService,
    RoutingMarketDataProvider,
)
from finagent.ledger import TransactionService
from finagent.llm import BailianModelProvider
from finagent.memory import (
    ContextAssembler,
    ConversationMessage,
    ConversationService,
    ConversationSummaryError,
    MemoryService,
    ModelConversationSummarizer,
    ModelMemoryCandidateExtractor,
)
from finagent.persistence import DatabaseManager, SqlAlchemyMemoryUnitOfWorkFactory
from finagent.persistence.unit_of_work import SqlAlchemyDashboardUnitOfWorkFactory
from finagent.portfolio import (
    Currency,
    InMemoryHoldingRepository,
    PortfolioCalculator,
    Quote,
)
from finagent.tools import create_read_only_asset_tool_registry

SUPPORTED_FUND_SYMBOLS = frozenset({"017811"})

AGENT_SYSTEM_PROMPT = """你是 FinAgent 的个人资产信息助手。
你可以通过只读工具查询当前组合快照、单项持仓和交易账本汇总。
涉及用户当前资产、行情、收益或交易事实时必须调用合适工具，不得猜测或编造。
工具只提供读取能力；你不能新增、修改或删除持仓和交易，也不能代替用户执行投资操作。
清楚区分事实、估算和一般性说明，引用行情时说明来源与时间；最终投资决定始终由用户作出。
"""


class _UnavailableConversationSummarizer:
    """无模型 Key 时保留会话管理能力，但明确拒绝模型摘要。

    此实现不会在正常的会话 CRUD 中被调用。聊天入口会更早返回 503；保留一个显式失败的
    Summarizer 只是为了让 ``ConversationService`` 在离线资产面板中仍可被完整装配。
    """

    async def summarize(
        self,
        _previous_summary: str | None,
        _messages: tuple[ConversationMessage, ...],
    ) -> str:
        """拒绝需要大模型的摘要请求，不生成伪造摘要。"""

        raise ConversationSummaryError("未配置 LLM_API_KEY，无法生成会话摘要")


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    """正式 FastAPI 应用在一个生命周期中共享的全部应用服务。"""

    dashboard: PortfolioDashboardService
    transactions: TransactionService
    conversations: ConversationService
    memories: MemoryService
    agent: AgentApplicationService | None
    memory_unit_of_work_factory: SqlAlchemyMemoryUnitOfWorkFactory
    agent_unavailable_reason: str | None = None

    async def initialize(self) -> None:
        """启动时验证资产与记忆服务使用的数据库结构均为当前版本。"""

        await self.dashboard.initialize()
        await self.memory_unit_of_work_factory.initialize()

    async def close(self) -> None:
        """先关闭共享模型客户端，再可靠释放行情和数据库资源。"""

        try:
            if self.agent is not None:
                await self.agent.close()
        finally:
            # Dashboard 的 UoW 与 Memory UoW 共享同一个 DatabaseManager，由一个明确
            # 所有者关闭即可；重复 dispose 没有价值，也会模糊资源所有权。
            await self.dashboard.close()


def _build_fake_quotes(now: datetime) -> tuple[Quote, ...]:
    """生成不包含真实用户信息、也不访问网络的固定演示行情。"""

    return (
        Quote.model_validate(
            {
                "symbol": "017811",
                "price": "4.00",
                "currency": "CNY",
                "as_of": now,
                "source": "Fake Provider 固定基金净值",
                "is_delayed": True,
            }
        ),
        Quote.model_validate(
            {
                "symbol": "XAU-CNY-GRAM",
                "price": "900.00",
                "currency": "CNY",
                "as_of": now,
                "source": "Fake Provider 固定国际黄金参考价",
                "is_delayed": True,
            }
        ),
    )


def build_market_data_service(settings: Settings) -> MarketDataService:
    """根据 Fake/Real 模式创建统一 ``MarketDataService``。"""

    provider: MarketDataProvider
    if settings.market_data_mode == "fake":
        provider = FakeMarketDataProvider(_build_fake_quotes(datetime.now(UTC)))
    else:
        # Settings 已保证 Real 模式配置 GoldAPI Key；Provider 仍只在收到请求时访问网络。
        provider = RoutingMarketDataProvider(
            fund_provider=AkShareFundNavProvider(),
            fund_symbols=SUPPORTED_FUND_SYMBOLS,
            gold_provider=GoldApiMarketDataProvider(settings),
        )
    return MarketDataService(provider)


def build_application_services(settings: Settings) -> ApplicationServices:
    """装配共享 SQLite、只读资产工具、持久化上下文和可选模型 Agent。"""

    database_manager = DatabaseManager(settings.database_path)
    dashboard_unit_of_work_factory = SqlAlchemyDashboardUnitOfWorkFactory(
        database_manager
    )
    memory_unit_of_work_factory = SqlAlchemyMemoryUnitOfWorkFactory(database_manager)

    dashboard = PortfolioDashboardService(
        dashboard_unit_of_work_factory,
        build_market_data_service(settings),
        PortfolioCalculator(Currency.CNY),
        manual_price_max_age=timedelta(seconds=settings.manual_gold_price_max_age_seconds),
        demo_enabled=settings.market_data_mode == "fake",
    )
    transactions = TransactionService(dashboard_unit_of_work_factory)
    memories = MemoryService(memory_unit_of_work_factory)

    if settings.llm_api_key is None:
        conversations = ConversationService(
            memory_unit_of_work_factory,
            _UnavailableConversationSummarizer(),
        )
        agent = None
        unavailable_reason = "未配置 LLM_API_KEY，Agent 聊天暂不可用"
    else:
        # 主 Agent、滚动摘要器和候选抽取器共享一个 Provider 连接池；只有最外层 Agent
        # ApplicationService 拥有关闭责任，其他适配器都不会重复关闭客户端。
        provider = BailianModelProvider(settings)
        conversations = ConversationService(
            memory_unit_of_work_factory,
            ModelConversationSummarizer(provider),
        )
        context_assembler = ContextAssembler(conversations, memories)
        persistent_agent = PersistentToolCallingAgent(
            provider,
            create_read_only_asset_tool_registry(dashboard, transactions),
            context_assembler,
            conversations,
            AGENT_SYSTEM_PROMPT,
        )
        agent = AgentApplicationService(
            persistent_agent,
            conversations,
            memories,
            ModelMemoryCandidateExtractor(provider),
        )
        unavailable_reason = None

    return ApplicationServices(
        dashboard=dashboard,
        transactions=transactions,
        conversations=conversations,
        memories=memories,
        agent=agent,
        memory_unit_of_work_factory=memory_unit_of_work_factory,
        agent_unavailable_reason=unavailable_reason,
    )


def build_dashboard_service(settings: Settings) -> PortfolioDashboardService:
    """只装配 Dashboard，避免脚本因本机存在模型 Key 而额外创建 Agent 客户端。"""

    unit_of_work_factory = SqlAlchemyDashboardUnitOfWorkFactory(
        DatabaseManager(settings.database_path)
    )
    return PortfolioDashboardService(
        unit_of_work_factory,
        build_market_data_service(settings),
        PortfolioCalculator(Currency.CNY),
        manual_price_max_age=timedelta(seconds=settings.manual_gold_price_max_age_seconds),
        demo_enabled=settings.market_data_mode == "fake",
    )


def build_in_memory_dashboard_service(settings: Settings) -> PortfolioDashboardService:
    """为自动测试和显式离线验收装配不会创建数据库文件的内存服务。"""

    holding_repository = InMemoryHoldingRepository()
    manual_price_repository = InMemoryManualPriceRepository()
    return PortfolioDashboardService(
        InMemoryDashboardUnitOfWorkFactory(
            holding_repository,
            manual_price_repository,
        ),
        build_market_data_service(settings),
        PortfolioCalculator(Currency.CNY),
        manual_price_max_age=timedelta(seconds=settings.manual_gold_price_max_age_seconds),
        demo_enabled=settings.market_data_mode == "fake",
    )
