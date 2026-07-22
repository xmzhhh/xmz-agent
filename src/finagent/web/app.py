"""FinAgent 资产面板的 FastAPI 应用工厂与 `/api/v1` 接口。

本模块只处理 HTTP 输入输出、状态码和生命周期。持仓规则、价格新鲜度、行情访问和金融公式
全部委托给应用服务，避免网页、CLI 和未来 Agent 工具各自实现一套不一致的计算逻辑。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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
from finagent.portfolio import (
    AssetDefinition,
    AssetNotHoldableError,
    DemoPortfolioConflictError,
    DuplicateHoldingError,
    Holding,
    HoldingCreate,
    HoldingNotFoundError,
    HoldingUpdate,
    PortfolioError,
    UnsupportedAssetError,
)
from finagent.web.composition import build_dashboard_service


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
        ),
    ):
        return status.HTTP_409_CONFLICT
    if isinstance(
        error,
        (UnsupportedAssetError, AssetNotHoldableError, ManualPriceNotSupportedError),
    ):
        return status.HTTP_422_UNPROCESSABLE_CONTENT
    return status.HTTP_422_UNPROCESSABLE_CONTENT


def create_app(
    settings: Settings | None = None,
    dashboard_service: PortfolioDashboardService | None = None,
) -> FastAPI:
    """创建一个拥有独立内存状态和明确关闭生命周期的 FastAPI 应用。"""

    active_settings = settings or get_settings()
    service = dashboard_service or build_dashboard_service(active_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """应用退出时沿 Service→Provider 链路释放外部资源。"""

        try:
            yield
        finally:
            await service.close()

    app = FastAPI(
        title="FinAgent Portfolio Dashboard",
        description="模拟持仓、手工京东卖价与可追溯行情的资产面板 API",
        lifespan=lifespan,
    )
    app.state.dashboard_service = service

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

        return await service.create_holding(data)

    @router.get("/holdings/{symbol}", response_model=Holding)
    async def get_holding(symbol: str) -> Holding:
        """按代码读取持仓。"""

        return await service.get_holding(symbol)

    @router.put("/holdings/{symbol}", response_model=Holding)
    async def update_holding(symbol: str, data: HoldingUpdate) -> Holding:
        """完整替换持仓的三个可编辑数值字段。"""

        return await service.update_holding(symbol, data)

    @router.delete("/holdings/{symbol}", response_model=Holding)
    async def delete_holding(symbol: str) -> Holding:
        """删除持仓，并由 Service 处理手工价格联动清理。"""

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

    @router.get("/health")
    async def health() -> dict[str, str]:
        """返回不访问外部行情的轻量进程健康状态。"""

        return {"status": "ok", "market_data_mode": active_settings.market_data_mode}

    app.include_router(router)
    return app
