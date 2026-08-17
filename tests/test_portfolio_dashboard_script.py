"""Phase 6 资产面板离线验收脚本测试。

测试直接调用脚本的异步核心函数，不打开本地端口、不读取开发者 ``.env``，也不访问任何真实
数据源。成功场景核对关键输出，失败场景证明缺失端点不会被误报为验收通过。
"""

import pytest
from fastapi import FastAPI

import scripts.step07_check_portfolio_dashboard as dashboard_script


@pytest.mark.asyncio
async def test_dashboard_script_checks_page_demo_and_manual_price(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """默认入口应完整验证页面资源、演示组合和手工黄金价格更新。"""

    succeeded = await dashboard_script.check_portfolio_dashboard()

    output = capsys.readouterr().out
    assert succeeded is True
    assert "运行模式：Fake" in output
    assert "页面资源：HTML、CSS、JavaScript 均可读取" in output
    assert "匿名演示持仓：('017811', 'JD-ZS-GOLD')" in output
    assert "组合毛市值：2100.00 CNY" in output
    assert "预计到账金额：2091.20 CNY" in output
    assert "手工黄金卖出价：875.50 CNY/克" in output
    assert "更新后组合毛市值：2151.00 CNY" in output
    assert "真实网络请求：无" in output
    assert "资产面板验收：通过" in output


@pytest.mark.asyncio
async def test_dashboard_script_reports_missing_endpoint_as_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """注入缺少 Phase 6 路由的应用时，脚本必须返回失败并说明状态码。"""

    succeeded = await dashboard_script.check_portfolio_dashboard(FastAPI())

    output = capsys.readouterr().out
    assert succeeded is False
    assert "DashboardAcceptanceError" in output
    assert "健康检查 状态码应为 200，实际为 404" in output
    assert "资产面板验收：失败" in output


def test_dashboard_script_main_completes_without_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """同步 main 应能运行默认 Fake 验收，并以正常退出表示成功。"""

    dashboard_script.main()

    assert "资产面板验收：通过" in capsys.readouterr().out
