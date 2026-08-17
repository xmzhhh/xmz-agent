"""Alembic 的异步迁移运行环境。

Alembic 本身以同步方式组织迁移步骤，但可以通过 SQLAlchemy AsyncEngine 建立连接，再用
``run_sync`` 把迁移函数放到该连接上执行。数据库 URL 始终来自应用 ``Settings``，确保
PyCharm、Dashboard 和迁移命令使用同一个本机文件。
"""

from asyncio import run
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from finagent.core.config import get_settings
from finagent.persistence.database import Base, install_sqlite_connection_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 后续新增 ORM 模型时，必须由 persistence 包导入模型，使它们注册到这份 metadata。
# Alembic autogenerate 只负责生成候选迁移，生成结果仍必须人工审查。
target_metadata = Base.metadata


def _database_url() -> str:
    """从类型安全配置构造 Alembic 使用的 URL 字符串。"""

    from finagent.persistence.database import build_sqlite_url

    return build_sqlite_url(get_settings().database_path).render_as_string(
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
