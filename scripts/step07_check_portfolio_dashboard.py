"""Step 07：离线验收 Phase 6 模拟持仓 API 与网页资产面板。

本脚本固定使用 Fake 模式，在内存中直接请求 FastAPI ASGI 应用，不打开端口、不读取开发者
``.env``，也不会访问 AKShare、GoldAPI 或百炼。它适合在 PyCharm 中直接运行，用可读输出证明
页面资源、匿名演示组合、手工黄金价格和确定性组合估值已经连成完整闭环。
"""

import asyncio
from typing import cast

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from finagent.core.config import Settings
from finagent.dashboard import DashboardSnapshot, ManualPriceRecord
from finagent.portfolio import AssetDefinition, Holding
from finagent.web import build_in_memory_dashboard_service, create_app

EXPECTED_ASSET_SYMBOLS = ("017811", "JD-ZS-GOLD", "XAU-CNY-GRAM")
EXPECTED_INITIAL_MARKET_VALUE = "2100.00"
EXPECTED_INITIAL_NET_VALUE = "2091.20"
UPDATED_GOLD_PRICE = "875.50"
EXPECTED_UPDATED_MARKET_VALUE = "2151.00"


class DashboardAcceptanceError(RuntimeError):
    """表示 HTTP 请求成功执行，但返回内容不符合 Phase 6 验收约定。"""


def _require(condition: bool, message: str) -> None:
    """在验收条件不满足时抛出带有具体原因的异常。"""

    if not condition:
        raise DashboardAcceptanceError(message)


def _require_status(response: httpx.Response, expected: int, label: str) -> None:
    """检查一个 HTTP 响应的状态码，并隐藏可能过长的响应正文。"""

    if response.status_code != expected:
        raise DashboardAcceptanceError(
            f"{label} 状态码应为 {expected}，实际为 {response.status_code}"
        )


async def check_portfolio_dashboard(app: FastAPI | None = None) -> bool:
    """执行一次不访问网络的资产面板端到端验收。

    Args:
        app: 可选 FastAPI 应用。省略时创建强制 Fake 模式的正式应用；测试可以注入缺少端点的
            应用，验证脚本能够明确报告失败。

    Returns:
        页面、静态资源、API、演示组合和手工价格更新全部符合约定时返回 ``True``；已分类的
        HTTP、响应模型或验收断言错误返回 ``False``。

    Notes:
        函数显式进入 FastAPI lifespan，退出时会关闭 Dashboard Service 和底层 Provider。
        编程错误不会被宽泛的 ``except Exception`` 隐藏。
    """

    settings = Settings.model_validate({"market_data_mode": "fake"})
    # Phase 6 验收必须可重复运行且不写个人数据库，因此显式注入内存 Unit of Work；正式
    # ``finagent dashboard`` 的组合根已经改用 SQLite。
    active_app = app or create_app(
        settings,
        build_in_memory_dashboard_service(settings),
    )

    print("=== Phase 6 模拟持仓与资产面板离线验收 ===")
    print("运行模式：Fake（固定匿名数据）")

    try:
        transport = httpx.ASGITransport(app=active_app)
        async with active_app.router.lifespan_context(active_app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                health_response = await client.get("/api/v1/health")
                _require_status(health_response, 200, "健康检查")
                health = cast(dict[str, str], health_response.json())
                _require(health.get("market_data_mode") == "fake", "健康检查没有返回 Fake 模式")

                page_response = await client.get("/")
                css_response = await client.get("/static/dashboard.css")
                script_response = await client.get("/static/dashboard.js")
                _require_status(page_response, 200, "资产面板页面")
                _require_status(css_response, 200, "资产面板 CSS")
                _require_status(script_response, 200, "资产面板 JavaScript")
                _require("FinAgent 资产面板" in page_response.text, "页面缺少标题")
                _require('id="holding-form"' in page_response.text, "页面缺少持仓表单")
                print("页面资源：HTML、CSS、JavaScript 均可读取")

                assets_response = await client.get("/api/v1/assets")
                _require_status(assets_response, 200, "资产目录")
                assets = tuple(
                    AssetDefinition.model_validate(item) for item in assets_response.json()
                )
                asset_symbols = tuple(asset.symbol for asset in assets)
                _require(asset_symbols == EXPECTED_ASSET_SYMBOLS, "资产目录代码或顺序不符合约定")
                print(f"资产目录：{asset_symbols}")

                empty_response = await client.get("/api/v1/holdings")
                _require_status(empty_response, 200, "空持仓查询")
                _require(empty_response.json() == [], "新建应用的持仓仓库应为空")

                demo_response = await client.post("/api/v1/demo")
                _require_status(demo_response, 201, "载入匿名演示组合")
                holdings = tuple(Holding.model_validate(item) for item in demo_response.json())
                _require(len(holdings) == 2, "匿名演示组合应包含两项持仓")
                print(f"匿名演示持仓：{tuple(holding.symbol for holding in holdings)}")

                dashboard_response = await client.get("/api/v1/dashboard")
                _require_status(dashboard_response, 200, "初始组合快照")
                dashboard = DashboardSnapshot.model_validate(dashboard_response.json())
                _require(
                    str(dashboard.portfolio.total_market_value) == EXPECTED_INITIAL_MARKET_VALUE,
                    "初始组合毛市值不符合固定演示数据",
                )
                _require(
                    str(dashboard.portfolio.total_net_liquidation_value)
                    == EXPECTED_INITIAL_NET_VALUE,
                    "初始组合预计到账金额不符合固定演示数据",
                )
                _require(
                    dashboard.gold_reference.status.value == "available",
                    "演示黄金持仓应取得 Fake 国际黄金参考价",
                )
                print(f"组合毛市值：{dashboard.portfolio.total_market_value} CNY")
                print(f"预计到账金额：{dashboard.portfolio.total_net_liquidation_value} CNY")
                print(f"集中度 HHI：{dashboard.portfolio.concentration_hhi}")

                price_response = await client.put(
                    "/api/v1/manual-prices/JD-ZS-GOLD",
                    json={"price": UPDATED_GOLD_PRICE},
                )
                _require_status(price_response, 200, "更新京东黄金手工卖出价")
                price_record = ManualPriceRecord.model_validate(price_response.json())
                _require(
                    str(price_record.price) == UPDATED_GOLD_PRICE,
                    "服务端没有保存预期手工黄金价格",
                )

                updated_response = await client.get("/api/v1/dashboard")
                _require_status(updated_response, 200, "更新后的组合快照")
                updated_dashboard = DashboardSnapshot.model_validate(updated_response.json())
                _require(
                    str(updated_dashboard.portfolio.total_market_value)
                    == EXPECTED_UPDATED_MARKET_VALUE,
                    "手工黄金价格更新后，组合毛市值没有按后端计算结果变化",
                )
                print(f"手工黄金卖出价：{price_record.price} CNY/克")
                print(f"更新后组合毛市值：{updated_dashboard.portfolio.total_market_value} CNY")

        print("真实网络请求：无")
        print("资产面板验收：通过")
        return True
    except (DashboardAcceptanceError, httpx.HTTPError, ValidationError) as error:
        print(f"错误类型：{type(error).__name__}")
        print(f"错误信息：{error}")
        print("资产面板验收：失败")
        return False


def main() -> None:
    """创建事件循环执行离线验收，并用非零退出码表示失败。"""

    succeeded = asyncio.run(check_portfolio_dashboard())
    if not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
