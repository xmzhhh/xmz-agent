"""异步 SQLite 基础设施测试。

这些测试只使用 pytest 创建的临时目录，不会读取或修改用户的真实 ``finagent.db``。
测试重点是路径、连接级安全设置、独立 Session 和资源关闭，而不是下一小阶段的业务表。
"""

from pathlib import Path

from sqlalchemy import text

from finagent.persistence import DatabaseManager, build_sqlite_url


def test_build_sqlite_url_supports_windows_path_with_spaces(tmp_path: Path) -> None:
    """Windows 盘符和空格必须由 SQLAlchemy URL 对象安全处理。"""

    database_path = tmp_path / "directory with spaces" / "finagent.db"

    url = build_sqlite_url(database_path)

    assert url.drivername == "sqlite+aiosqlite"
    assert url.database == str(database_path)


async def test_database_manager_creates_file_and_enables_foreign_keys(
    tmp_path: Path,
) -> None:
    """首次连接应创建父目录，并对实际连接启用 SQLite 外键检查。"""

    database_path = tmp_path / "nested" / "finagent.db"
    manager = DatabaseManager(database_path)

    try:
        await manager.check_connection()

        assert database_path.is_file()
        async with manager.session() as session:
            foreign_keys = await session.scalar(text("PRAGMA foreign_keys"))
            busy_timeout = await session.scalar(text("PRAGMA busy_timeout"))

        assert foreign_keys == 1
        assert busy_timeout == 5000
    finally:
        await manager.close()


async def test_database_manager_sessions_are_not_shared(tmp_path: Path) -> None:
    """不同业务操作必须获得不同 Session，防止并发请求互相污染事务状态。"""

    manager = DatabaseManager(tmp_path / "finagent.db")

    try:
        await manager.check_connection()
        async with manager.session() as first_session:
            async with manager.session() as second_session:
                assert first_session is not second_session
    finally:
        await manager.close()
