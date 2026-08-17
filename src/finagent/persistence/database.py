"""异步 SQLite Engine、Session 工厂和连接生命周期。

这里仅建立数据库基础设施，不创建业务表，也不调用 ``metadata.create_all``。正式数据库
结构必须通过 Alembic 迁移演进，否则开发者无法追踪“某一版代码需要什么表结构”。
Repository 在下一小阶段通过 :class:`DatabaseManager` 获取独立的 ``AsyncSession``。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, event, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# 显式的约束命名让 Alembic 生成稳定、可读的迁移。若完全依赖 SQLite 自动命名，未来修改
# 外键或唯一约束时，不同数据库后端可能得到不同名字，迁移脚本也更难审查。
CONSTRAINT_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 SQLAlchemy ORM 模型共享的声明式基类。"""

    metadata = MetaData(naming_convention=CONSTRAINT_NAMING_CONVENTION)


def build_sqlite_url(database_path: Path) -> URL:
    """安全构造适用于 Windows 路径的异步 SQLite URL。

    不手工拼接 ``sqlite+aiosqlite:///...``，是因为盘符、反斜杠、空格等字符很容易导致
    URL 解析错误。SQLAlchemy 的 :class:`URL` 会把文件系统路径放在正确的字段中。

    Args:
        database_path: 已解析的 SQLite 数据库绝对路径。

    Returns:
        使用 ``aiosqlite`` 驱动的 SQLAlchemy URL。
    """

    return URL.create("sqlite+aiosqlite", database=str(database_path))


def _enable_sqlite_safety_pragmas(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    """为连接池创建的每一条 SQLite 连接启用安全设置。

    SQLite 的外键约束默认关闭，而且该设置只对当前连接有效，所以不能只在程序启动时
    执行一次。事件监听器确保连接池以后新建的连接也会执行相同配置。忙等待超时可以减少
    两个短写入恰好重叠时立刻报 ``database is locked`` 的概率，但它不能代替事务设计。
    """

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def install_sqlite_connection_settings(engine: AsyncEngine) -> None:
    """把 SQLite 连接设置注册到异步 Engine 底层的同步连接池。"""

    event.listen(engine.sync_engine, "connect", _enable_sqlite_safety_pragmas)


class DatabaseManager:
    """统一拥有异步 Engine、Session 工厂和关闭职责。

    ``AsyncSession`` 是有状态的事务对象，不能在多个并发请求之间共享。因此这里只共享
    轻量的 Session 工厂，每次 Repository 或业务事务都创建自己的 Session。应用退出时
    必须调用 :meth:`close`，让连接池释放文件句柄。

    Args:
        database_path: SQLite 数据库文件的绝对路径。
        echo: 是否把 SQL 输出到日志；仅用于本地排查，默认关闭以免泄露业务数据。
    """

    def __init__(self, database_path: Path, *, echo: bool = False) -> None:
        self._database_path = database_path
        self._engine = create_async_engine(
            build_sqlite_url(database_path),
            echo=echo,
        )
        install_sqlite_connection_settings(self._engine)
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def database_path(self) -> Path:
        """返回当前 Manager 管理的数据库文件路径。"""

        return self._database_path

    @property
    def engine(self) -> AsyncEngine:
        """向 Alembic 集成或低层诊断暴露异步 Engine。"""

        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """为一次 Repository 操作提供独立 Session。

        本方法只负责创建和关闭 Session，不自动提交事务。写操作必须显式使用
        ``session.begin()`` 或由后续 Unit of Work 统一提交，避免异常发生后仍误提交半成品。
        """

        async with self._session_factory() as session:
            yield session

    async def check_connection(self) -> None:
        """创建父目录并执行最小查询，尽早暴露路径或权限错误。

        该检查不会创建业务表。数据库结构仍必须通过 Alembic 迁移建立。
        """

        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        """释放连接池和 SQLite 文件句柄；应用生命周期结束时调用。"""

        await self._engine.dispose()
