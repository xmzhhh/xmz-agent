"""Uvicorn 资产面板服务器的同步启动边界。"""

import uvicorn


def run_dashboard_server(host: str, port: int) -> None:
    """启动 FastAPI 应用工厂；阻塞直到用户停止服务器。"""

    uvicorn.run(
        "finagent.web.app:create_app",
        factory=True,
        host=host,
        port=port,
    )
