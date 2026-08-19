"""Alembic 的异步迁移运行环境。

Alembic 本身以同步方式组织迁移步骤，但可以通过 SQLAlchemy AsyncEngine 建立连接，再用
``run_sync`` 把迁移函数放到该连接上执行。数据库 URL 始终来自应用 ``Settings``，确保
PyCharm、Dashboard 和迁移命令使用同一个本机文件。
"""

from asyncio import run
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from finagent.core.config import Settings
from finagent.persistence import Base, UTCDateTime
from finagent.persistence.database import install_sqlite_connection_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 后续新增 ORM 模型时，必须由 persistence 包导入模型，使它们注册到这份 metadata。
# Alembic autogenerate 只负责生成候选迁移，生成结果仍必须人工审查。
target_metadata = Base.metadata


def _render_migration_item(
    item_type: str,
    item: Any,
    _autogenerate_context: Any,
) -> str | bool:
    """让自定义 UTC 类型在迁移中保存为稳定的数据库物理类型。

    ``UTCDateTime`` 的时区转换属于应用读写逻辑，SQLite 中真正创建的仍是 ``DATETIME``。
    历史迁移不应依赖以后可能移动或重命名的应用类，因此自动生成时写成 SQLAlchemy 标准
    类型。返回 ``False`` 表示其他对象继续使用 Alembic 默认渲染方式。
    """

    if item_type == "type" and isinstance(item, UTCDateTime):
        return "sa.DateTime()"
    return False


def _database_url() -> str:
    """从类型安全配置构造 Alembic 使用的 URL 字符串。"""

    from finagent.persistence.database import build_sqlite_url

    # 迁移命令是一次性进程，不使用应用级 lru_cache。这样同一测试进程切换临时数据库时，
    # 每次 Alembic 调用都会重新读取 DATABASE_PATH，不会误连接用户的真实数据库。
    return build_sqlite_url(Settings().database_path).render_as_string(
        hide_password=False
    )


def run_migrations_offline() -> None:
    """在不创建数据库连接时生成 SQL 脚本。"""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=_render_migration_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    """在 AsyncEngine 提供的同步适配连接中执行迁移。"""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,
        render_item=_render_migration_item,
    )

    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    """建立异步连接，并把实际迁移委托给同步 Alembic 上下文。"""

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    install_sqlite_connection_settings(connectable)

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """为 Alembic 的同步入口启动一次异步迁移事件循环。"""

    run(_run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
