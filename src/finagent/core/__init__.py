"""FinAgent 的核心基础设施。

该包放置会被多个业务模块共同使用的配置、通用数据结构和异常类型。
它不应该依赖具体的 Agent、工具或界面实现，从而避免产生循环依赖。
"""

from finagent.core.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
