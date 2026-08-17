"""FastAPI ``/api/v1`` 的离线端点、错误结构和生命周期测试。

所有测试显式注入 Fake Provider 与内存仓库，不读取开发者 ``.env``，也不会访问
AKShare、GoldAPI 或百炼。HTTP 层只验证传输、状态码和生命周期；金融结果仍由真实的
``PortfolioCalculator`` 产生。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from finagent.core.config import Settings
from finagent.dashboard import InMemoryManualPriceRepository, PortfolioDashboardService
from finagent.data import FakeMarketDataProvider, MarketDataClosedError, MarketDataService
from finagent.portfolio import (
    Currency,
    InMemoryHoldingRepository,
    PortfolioCalculator,
    Quote,
)
from finagent.web.app import create_app

NOW = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)


def make_quote(symbol: str, price: str) -> Quote:
    """构造 API 测试使用的固定人民币行情。"""

    return Quote.model_validate(
        {
            "symbol": symbol,
            "price": price,
            "currency": "CNY",
            "as_of": NOW,
            "source": "Web API Fake Provider",
            "is_delayed": False,
        }
    )


@asynccontextmanager
async def open_test_client(
    quotes: tuple[Quote, ...] = (),
    *,
    provider_latency: float = 0,
    request_timeout: float = 5,
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeMarketDataProvider]]:
    """创建共享一次应用生命周期的异步测试客户端。

    ``ASGITransport`` 会把 HTTP 请求直接交给内存中的 FastAPI 应用，不打开真实端口。
    httpx 不会自动触发 ASGI lifespan，因此测试显式进入应用的生命周期上下文，确保
    退出时会沿 ``DashboardService → MarketDataService → Provider`` 链路关闭资源。
    """

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = FakeMarketDataProvider(quotes, latency_seconds=provider_latency)
    service = PortfolioDashboardService(
        InMemoryHoldingRepository(),
        InMemoryManualPriceRepository(),
        MarketDataService(provider, request_timeout_seconds=request_timeout),
        PortfolioCalculator(Currency.CNY),
        manual_price_max_age=timedelta(seconds=900),
        clock=lambda: NOW,
        demo_enabled=True,
    )
    app = create_app(settings, service)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client, provider


async def test_health_and_assets_do_not_require_llm_key() -> None:
    """无模型 Key 时健康检查和完整资产目录仍应正常返回。"""

    async with open_test_client() as (client, _):
        health = await client.get("/api/v1/health")
        assets = await client.get("/api/v1/assets")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "market_data_mode": "fake"}
    assert assets.status_code == 200
    assert [asset["symbol"] for asset in assets.json()] == [
        "017811",
        "JD-ZS-GOLD",
        "XAU-CNY-GRAM",
    ]


async def test_dashboard_page_and_static_assets_are_served() -> None:
    """网页外壳、CSS 和 JavaScript 应由安装包内的同一个 FastAPI 应用提供。"""

    async with open_test_client() as (client, _):
        page = await client.get("/")
        stylesheet = await client.get("/static/dashboard.css")
        script = await client.get("/static/dashboard.js")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>FinAgent 资产面板</title>" in page.text
    assert 'data-market-mode="fake"' in page.text
    assert 'id="holding-form"' in page.text
    assert 'id="positions-body"' in page.text
    assert "/static/dashboard.css" in page.text
    assert "/static/dashboard.js" in page.text

    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "--accent:" in stylesheet.text

    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert 'const API_BASE = "/api/v1"' in script.text
    assert 'apiRequest("/dashboard")' in script.text
    # 外部行情来源通过 textContent 写入，防止把 Provider 文本作为 HTML 执行。
    assert ".innerHTML" not in script.text


async def test_holding_crud_uses_decimal_strings_and_consistent_not_found_error() -> None:
    """持仓 CRUD 应保持 Decimal 字符串，并把不存在映射为统一 404 结构。"""

    create_payload = {
        "symbol": "017811",
        "quantity": "100",
        "average_cost": "3.50",
        "estimated_exit_fee_percent": "0.50",
    }
    update_payload = {
        "quantity": "120",
        "average_cost": "3.60",
        "estimated_exit_fee_percent": "0.25",
    }

    async with open_test_client() as (client, _):
        created = await client.post("/api/v1/holdings", json=create_payload)
        listed = await client.get("/api/v1/holdings")
        updated = await client.put("/api/v1/holdings/017811", json=update_payload)
        deleted = await client.delete("/api/v1/holdings/017811")
        missing = await client.get("/api/v1/holdings/017811")

    assert created.status_code == 201
    assert created.json()["quantity"] == "100"
    assert listed.json()[0]["average_cost"] == "3.50"
    assert updated.json()["quantity"] == "120"
    assert deleted.status_code == 200
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "HoldingNotFoundError"


async def test_request_validation_and_unsupported_holding_use_422() -> None:
    """float 金融输入和仅供参考资产都应在进入仓库前返回 422。"""

    async with open_test_client() as (client, _):
        invalid_decimal = await client.post(
            "/api/v1/holdings",
            json={"symbol": "017811", "quantity": 100.5, "average_cost": "3.50"},
        )
        reference_asset = await client.post(
            "/api/v1/holdings",
            json={
                "symbol": "XAU-CNY-GRAM",
                "quantity": "1",
                "average_cost": "900",
            },
        )

    assert invalid_decimal.status_code == 422
    assert invalid_decimal.json()["error"]["code"] == "request_validation_error"
    assert reference_asset.status_code == 422
    assert reference_asset.json()["error"]["code"] == "AssetNotHoldableError"


async def test_duplicate_holding_returns_409_without_overwrite() -> None:
    """重复 POST 不得覆盖原持仓，应返回 409 冲突。"""

    payload = {"symbol": "017811", "quantity": "100", "average_cost": "3.50"}
    async with open_test_client() as (client, _):
        assert (await client.post("/api/v1/holdings", json=payload)).status_code == 201
        duplicate = await client.post("/api/v1/holdings", json=payload)
        current = await client.get("/api/v1/holdings/017811")

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DuplicateHoldingError"
    assert current.json()["quantity"] == "100"


async def test_manual_price_endpoints_use_server_time_and_delete_linkage() -> None:
    """手工价格 API 不接受客户端时间，删除黄金持仓后旧价格应一起消失。"""

    gold = {"symbol": "JD-ZS-GOLD", "quantity": "2", "average_cost": "800"}
    async with open_test_client() as (client, _):
        await client.post("/api/v1/holdings", json=gold)
        saved = await client.put(
            "/api/v1/manual-prices/JD-ZS-GOLD",
            json={"price": "850"},
        )
        fetched = await client.get("/api/v1/manual-prices/JD-ZS-GOLD")
        await client.delete("/api/v1/holdings/JD-ZS-GOLD")
        missing = await client.get("/api/v1/manual-prices/JD-ZS-GOLD")

    assert saved.status_code == 200
    assert saved.json()["price"] == "850"
    assert saved.json()["recorded_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert fetched.json() == saved.json()
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "ManualPriceNotFoundError"


async def test_demo_and_dashboard_return_complete_serialized_snapshot() -> None:
    """Fake 演示组合应通过真实 Service 和 Calculator 生成可序列化资产快照。"""

    quotes = (make_quote("017811", "4.00"), make_quote("XAU-CNY-GRAM", "900"))
    async with open_test_client(quotes) as (client, provider):
        demo = await client.post("/api/v1/demo")
        dashboard = await client.get("/api/v1/dashboard")

        payload = dashboard.json()
        assert demo.status_code == 201
        assert dashboard.status_code == 200
        assert payload["portfolio"]["total_market_value"] == "2100.00"
        assert payload["portfolio"]["total_net_liquidation_value"] == "2091.20"
        assert payload["gold_reference"]["status"] == "available"
        assert provider.requested_symbols == ("017811", "XAU-CNY-GRAM")


async def test_missing_manual_price_returns_409_before_market_request() -> None:
    """黄金手工价缺失属于状态冲突，且不能提前消耗任何行情请求。"""

    async with open_test_client((make_quote("XAU-CNY-GRAM", "900"),)) as (
        client,
        provider,
    ):
        await client.post(
            "/api/v1/holdings",
            json={"symbol": "JD-ZS-GOLD", "quantity": "2", "average_cost": "800"},
        )
        dashboard = await client.get("/api/v1/dashboard")

        assert dashboard.status_code == 409
        assert dashboard.json()["error"]["code"] == "ManualPriceNotFoundError"
        assert provider.requested_symbols == ()


async def test_required_market_failure_maps_to_503() -> None:
    """必要基金行情不可用时应返回 503，而不是生成残缺快照。"""

    async with open_test_client() as (client, provider):
        await client.post(
            "/api/v1/holdings",
            json={"symbol": "017811", "quantity": "100", "average_cost": "3.50"},
        )
        dashboard = await client.get("/api/v1/dashboard")

        assert dashboard.status_code == 503
        assert dashboard.json()["error"]["code"] == "MarketDataNotFoundError"
        assert provider.requested_symbols == ("017811",)


async def test_required_market_timeout_maps_to_504() -> None:
    """必要行情超过统一超时应返回 504，供前端区别普通服务不可用。"""

    async with open_test_client(
        (make_quote("017811", "4.00"),),
        provider_latency=0.05,
        request_timeout=0.001,
    ) as (client, _):
        await client.post(
            "/api/v1/holdings",
            json={"symbol": "017811", "quantity": "100", "average_cost": "3.50"},
        )
        dashboard = await client.get("/api/v1/dashboard")

    assert dashboard.status_code == 504
    assert dashboard.json()["error"]["code"] == "MarketDataTimeoutError"


async def test_app_lifespan_closes_injected_provider() -> None:
    """应用退出 lifespan 后应沿 Service 链路关闭 Fake Provider。"""

    async with open_test_client((make_quote("017811", "4.00"),)) as (client, provider):
        assert (await client.get("/api/v1/health")).status_code == 200

    assert provider.requested_symbols == ()
    with pytest.raises(MarketDataClosedError):
        await provider.get_quote("017811")
