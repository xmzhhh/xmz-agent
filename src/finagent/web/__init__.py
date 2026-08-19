"""FinAgent FastAPI 资产面板的公共入口。"""

from finagent.web.app import create_app
from finagent.web.composition import (
    ApplicationServices,
    build_application_services,
    build_dashboard_service,
    build_in_memory_dashboard_service,
    build_market_data_service,
)
from finagent.web.server import run_dashboard_server

__all__ = [
    "ApplicationServices",
    "build_application_services",
    "build_dashboard_service",
    "build_in_memory_dashboard_service",
    "build_market_data_service",
    "create_app",
    "run_dashboard_server",
]
