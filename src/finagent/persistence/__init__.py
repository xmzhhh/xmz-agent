"""FinAgent 的本地持久化基础设施。

本包负责把数据库连接、会话和迁移细节隔离在应用边界之外。Portfolio、Dashboard 等
业务模块仍然依赖 Repository 协议，不直接操作 SQLAlchemy；后续替换数据库时，上层
业务规则无需跟着重写。
"""

from finagent.persistence.database import Base, DatabaseManager, build_sqlite_url

__all__ = ["Base", "DatabaseManager", "build_sqlite_url"]
