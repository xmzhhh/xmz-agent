"""FinAgent 资产面板的 FastAPI 应用工厂与 `/api/v1` 接口。

本模块只处理 HTTP 输入输出、状态码和生命周期。持仓规则、价格新鲜度、行情访问和金融公式
全部委托给应用服务，避免网页、CLI 和未来 Agent 工具各自实现一套不一致的计算逻辑。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from finagent.agents import AgentApplicationService, AgentError
from finagent.core.config import Settings, get_settings
from finagent.dashboard import (
    DashboardSnapshot,
    DemoPortfolioUnavailableError,
    ManualPriceInput,
    ManualPriceNotFoundError,
    ManualPriceNotSupportedError,
    ManualPriceRecord,
    ManualPriceStaleError,
    PortfolioDashboardService,
)
from finagent.data import MarketDataError, MarketDataTimeoutError
from finagent.ledger import (
    BuyRequest,
    BuyResult,
    FutureTransactionError,
    InsufficientHoldingError,
    InvalidTradeFeeError,
    LedgerAlreadyInitializedError,
    LedgerManagedHoldingError,
    LedgerStateConflictError,
    LedgerTransaction,
    NonChronologicalTransactionError,
    OpeningPositionRequest,
    OpeningPositionResult,
    RealizedPnlSummary,
    SellPreview,
    SellRequest,
    SellResult,
    TransactionService,
    UntrackedHoldingError,
)
from finagent.llm import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelProviderError,
    ModelRateLimitError,
    ModelResponseError,
    ModelTimeoutError,
)
from finagent.memory import (
    ConversationArchivedError,
    ConversationConflictError,
    ConversationMessageNotFoundError,
    ConversationNotFoundError,
    ConversationService,
    InvalidMemoryTransitionError,
    MemoryCandidateExpiredError,
    MemoryDomainError,
    MemoryItemNotFoundError,
    MemoryService,
)
from finagent.portfolio import (
    AssetDefinition,
    AssetNotHoldableError,
    Currency,
    DemoPortfolioConflictError,
    DuplicateHoldingError,
    Holding,
    HoldingCreate,
    HoldingNotFoundError,
    HoldingUpdate,
    PortfolioError,
    UnsupportedAssetError,
)
from finagent.web.agent_api import AgentUnavailableError, create_agent_router
from finagent.web.composition import build_application_services

WEB_DIRECTORY = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=WEB_DIRECTORY / "templates")


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """构造所有 API 错误共享的稳定 JSON 结构。"""

    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _portfolio_error_status(error: PortfolioError) -> int:
    """把稳定领域异常映射为 HTTP 语义，不让路由函数重复判断。"""

    if isinstance(error, HoldingNotFoundError):
        return status.HTTP_404_NOT_FOUND
    if isinstance(
        error,
        (
            DuplicateHoldingError,
            DemoPortfolioConflictError,
            DemoPortfolioUnavailableError,
            ManualPriceNotFoundError,
            ManualPriceStaleError,
            LedgerAlreadyInitializedError,
            LedgerManagedHoldingError,
            LedgerStateConflictError,
            InsufficientHoldingError,
            NonChronologicalTransactionError,
            UntrackedHoldingError,
        ),
    ):
        return status.HTTP_409_CONFLICT
    if isinstance(
        error,
        (
            UnsupportedAssetError,
            AssetNotHoldableError,
            ManualPriceNotSupportedError,
            InvalidTradeFeeError,
            FutureTransactionError,
        ),
    ):
        return status.HTTP_422_UNPROCESSABLE_CONTENT
    return status.HTTP_422_UNPROCESSABLE_CONTENT


def create_app(
    settings: Settings | None = None,
    dashboard_service: PortfolioDashboardService | None = None,
    transaction_service: TransactionService | None = None,
    conversation_service: ConversationService | None = None,
    memory_service: MemoryService | None = None,
    agent_service: AgentApplicationService | None = None,
) -> FastAPI:
    """创建拥有显式存储初始化、共享 Agent 和记忆接口的 FastAPI 应用。

    生产入口省略全部 Service 参数，由组合根装配同一 SQLite。测试可以继续只注入旧资产
    Service，也可以显式注入 Fake Agent 依赖，且不会读取开发者的 API Key。
    """

    active_settings = settings or get_settings()
    application_services = None
    active_transaction_service: TransactionService | None
    active_conversation_service: ConversationService | None
    active_memory_service: MemoryService | None
    active_agent_service: AgentApplicationService | None
    agent_unavailable_reason: str | None
    if dashboard_service is None:
        application_services = build_application_services(active_settings)
        service = application_services.dashboard
        active_transaction_service = application_services.transactions
        active_conversation_service = application_services.conversations
        active_memory_service = application_services.memories
        active_agent_service = application_services.agent
        agent_unavailable_reason = application_services.agent_unavailable_reason
    else:
        service = dashboard_service
        active_transaction_service = transaction_service
        active_conversation_service = conversation_service
        active_memory_service = memory_service
        active_agent_service = agent_service
        agent_unavailable_reason = None

    def require_transaction_service() -> TransactionService:
        """测试若只注入旧 Dashboard Service，访问交易端点时快速暴露装配错误。"""

        if active_transaction_service is None:
            raise RuntimeError("当前应用实例未装配 TransactionService")
        return active_transaction_service

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """启动时检查存储，退出时沿 Service 链路释放数据库和 Provider。"""

        if application_services is not None:
            try:
                await application_services.initialize()
                yield
            finally:
                await application_services.close()
        else:
            try:
                await service.initialize()
                yield
            finally:
                try:
                    if active_agent_service is not None:
                        await active_agent_service.close()
                finally:
                    await service.close()

    app = FastAPI(
        title="FinAgent Portfolio Dashboard",
        description="模拟持仓、手工京东卖价与可追溯行情的资产面板 API",
        lifespan=lifespan,
    )
    app.state.dashboard_service = service
    app.state.transaction_service = active_transaction_service
    app.state.conversation_service = active_conversation_service
    app.state.memory_service = active_memory_service
    app.state.agent_service = active_agent_service

    # 静态目录和模板都位于 Python 包内部，安装 wheel 后仍能由同一应用入口提供。
    app.mount(
        "/static",
        StaticFiles(directory=WEB_DIRECTORY / "static"),
        name="static",
    )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard_page(request: Request) -> Response:
        """返回资产面板外壳；所有实时数据由浏览器继续请求 `/api/v1`。"""

        return TEMPLATES.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={"market_data_mode": active_settings.market_data_mode},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        """把 FastAPI 请求校验错误转换为统一结构，不回显完整请求正文。"""

        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "request_validation_error",
            "请求参数校验失败，请检查字段、数据类型和取值范围",
        )

    @app.exception_handler(PortfolioError)
    async def handle_portfolio_error(
        _request: Request,
        error: PortfolioError,
    ) -> JSONResponse:
        """映射持仓、手工价格和 Dashboard 业务异常。"""

        return _error_response(
            _portfolio_error_status(error),
            error.__class__.__name__,
            str(error),
        )

    @app.exception_handler(MarketDataError)
    async def handle_market_data_error(
        _request: Request,
        error: MarketDataError,
    ) -> JSONResponse:
        """超时使用 504，其他必要行情不可用使用 503。"""

        status_code = (
            status.HTTP_504_GATEWAY_TIMEOUT
            if isinstance(error, MarketDataTimeoutError)
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return _error_response(status_code, error.__class__.__name__, str(error))

    @app.exception_handler(AgentUnavailableError)
    async def handle_agent_unavailable(
        _request: Request,
        error: AgentUnavailableError,
    ) -> JSONResponse:
        """缺少模型配置只影响聊天入口，不影响资产、会话和记忆管理。"""

        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            error.__class__.__name__,
            str(error),
        )

    @app.exception_handler(MemoryDomainError)
    async def handle_memory_error(
        _request: Request,
        error: MemoryDomainError,
    ) -> JSONResponse:
        """把会话、记忆状态机异常映射为稳定 HTTP 语义。"""

        if isinstance(
            error,
            (
                ConversationNotFoundError,
                ConversationMessageNotFoundError,
                MemoryItemNotFoundError,
            ),
        ):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(
            error,
            (
                ConversationArchivedError,
                ConversationConflictError,
                InvalidMemoryTransitionError,
                MemoryCandidateExpiredError,
            ),
        ):
            status_code = status.HTTP_409_CONFLICT
        else:
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        return _error_response(status_code, error.__class__.__name__, str(error))

    @app.exception_handler(ModelProviderError)
    async def handle_model_error(
        _request: Request,
        error: ModelProviderError,
    ) -> JSONResponse:
        """区分模型限流、超时、连接、鉴权和响应协议错误。"""

        if isinstance(error, ModelRateLimitError):
            status_code = status.HTTP_429_TOO_MANY_REQUESTS
        elif isinstance(error, ModelTimeoutError):
            status_code = status.HTTP_504_GATEWAY_TIMEOUT
        elif isinstance(error, ModelConnectionError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        elif isinstance(error, ModelAuthenticationError):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        elif isinstance(error, ModelResponseError):
            status_code = status.HTTP_502_BAD_GATEWAY
        else:
            status_code = status.HTTP_502_BAD_GATEWAY
        return _error_response(status_code, error.__class__.__name__, str(error))

    @app.exception_handler(AgentError)
    async def handle_agent_error(
        _request: Request,
        error: AgentError,
    ) -> JSONResponse:
        """Agent 无法在安全边界内形成最终回答时返回上游响应错误。"""

        return _error_response(
            status.HTTP_502_BAD_GATEWAY,
            error.__class__.__name__,
            str(error),
        )

    router = APIRouter(prefix="/api/v1")

    @router.get("/assets", response_model=list[AssetDefinition])
    async def list_assets() -> tuple[AssetDefinition, ...]:
        """返回完整资产目录，包括不能录入持仓的国际黄金参考项。"""

        return service.list_assets()

    @router.get("/holdings", response_model=list[Holding])
    async def list_holdings() -> tuple[Holding, ...]:
        """返回代码排序后的全部持仓。"""

        return await service.list_holdings()

    @router.post(
        "/holdings",
        response_model=Holding,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_holding(data: HoldingCreate) -> Holding:
        """创建一项由资产目录补全元数据的持仓。"""

        if active_transaction_service is not None:
            await active_transaction_service.ensure_snapshot_editable(data.symbol)
        return await service.create_holding(data)

    @router.get("/holdings/{symbol}", response_model=Holding)
    async def get_holding(symbol: str) -> Holding:
        """按代码读取持仓。"""

        return await service.get_holding(symbol)

    @router.put("/holdings/{symbol}", response_model=Holding)
    async def update_holding(symbol: str, data: HoldingUpdate) -> Holding:
        """完整替换持仓的三个可编辑数值字段。"""

        if active_transaction_service is not None:
            await active_transaction_service.ensure_snapshot_editable(symbol)
        return await service.update_holding(symbol, data)

    @router.delete("/holdings/{symbol}", response_model=Holding)
    async def delete_holding(symbol: str) -> Holding:
        """删除持仓，并由 Service 处理手工价格联动清理。"""

        if active_transaction_service is not None:
            await active_transaction_service.ensure_snapshot_editable(symbol)
        return await service.delete_holding(symbol)

    @router.get("/manual-prices/{symbol}", response_model=ManualPriceRecord)
    async def get_manual_price(symbol: str) -> ManualPriceRecord:
        """读取手工价格记录，包括可能已经过期的旧记录。"""

        return await service.get_manual_price(symbol)

    @router.put("/manual-prices/{symbol}", response_model=ManualPriceRecord)
    async def set_manual_price(symbol: str, data: ManualPriceInput) -> ManualPriceRecord:
        """使用服务端时间新增或替换手工卖出价。"""

        return await service.set_manual_price(symbol, data)

    @router.delete("/manual-prices/{symbol}", response_model=ManualPriceRecord)
    async def delete_manual_price(symbol: str) -> ManualPriceRecord:
        """删除手工价格。"""

        return await service.delete_manual_price(symbol)

    @router.get("/dashboard", response_model=DashboardSnapshot)
    async def get_dashboard() -> DashboardSnapshot:
        """返回必要数据完整、参考价可降级的资产面板快照。"""

        return await service.get_dashboard()

    @router.post(
        "/demo",
        response_model=list[Holding],
        status_code=status.HTTP_201_CREATED,
    )
    async def load_demo() -> tuple[Holding, ...]:
        """仅在 Fake 模式和空状态下载入匿名演示组合。"""

        return await service.load_demo()

    @router.post(
        "/transactions/opening",
        response_model=OpeningPositionResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def initialize_opening_position(
        data: OpeningPositionRequest,
    ) -> OpeningPositionResult:
        """为旧持仓建立明确的期初流水和 FIFO 批次。"""

        return await require_transaction_service().initialize_opening_position(data)

    @router.post(
        "/transactions/buy",
        response_model=BuyResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def record_buy(data: BuyRequest) -> BuyResult:
        """确认一笔买入或加仓，并原子更新账本与当前持仓。"""

        return await require_transaction_service().record_buy(data)

    @router.post("/transactions/sell-preview", response_model=SellPreview)
    async def preview_sell(data: SellRequest) -> SellPreview:
        """只读计算卖出到账、FIFO 成本和预计已实现收益。"""

        return await require_transaction_service().preview_sell(data)

    @router.post(
        "/transactions/sell",
        response_model=SellResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def record_sell(data: SellRequest) -> SellResult:
        """用户确认试算后，重新校验当前状态并正式记录卖出。"""

        return await require_transaction_service().record_sell(data)

    @router.get("/transactions", response_model=list[LedgerTransaction])
    async def list_transactions(symbol: str | None = None) -> tuple[LedgerTransaction, ...]:
        """按时间查询全部或单项资产的不可变交易历史。"""

        return await require_transaction_service().list_transactions(symbol)

    @router.get("/transactions/realized-pnl", response_model=RealizedPnlSummary)
    async def get_realized_pnl(symbol: str | None = None) -> RealizedPnlSummary:
        """返回已确认卖出产生的已实现收益，不包含当前浮盈亏。"""

        amount = await require_transaction_service().get_realized_pnl(symbol)
        return RealizedPnlSummary(
            symbol=symbol.strip().upper() if symbol is not None else None,
            realized_pnl=amount,
            currency=Currency.CNY,
        )

    @router.get("/health")
    async def health() -> dict[str, str]:
        """返回不访问外部行情的轻量进程健康状态。"""

        return {"status": "ok", "market_data_mode": active_settings.market_data_mode}

    app.include_router(router)
    if active_conversation_service is not None and active_memory_service is not None:
        app.include_router(
            create_agent_router(
                active_conversation_service,
                active_memory_service,
                active_agent_service,
                unavailable_reason=agent_unavailable_reason,
            )
        )
    return app
