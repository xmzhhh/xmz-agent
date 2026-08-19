"""Phase 7 SQLite 持久化与交易账本离线验收脚本测试。

测试允许脚本创建系统临时数据库并执行真实 Alembic 迁移，但行情始终使用 Fake Provider。
它验证服务重启、交易闭环和失败报告，同时证明脚本不会改动环境变量指向的个人数据库。
"""

from pathlib import Path

import pytest
from alembic.util.exc import CommandError

import scripts.step08_check_persistence_ledger as ledger_script


@pytest.mark.asyncio
async def test_script_checks_migration_restarts_and_transaction_cycle(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认入口应在临时数据库中完成两次重启和完整交易闭环。"""

    personal_database = tmp_path / "must-not-be-created.db"
    monkeypatch.setenv("DATABASE_PATH", str(personal_database))

    succeeded = await ledger_script.check_persistence_ledger()

    output = capsys.readouterr().out
    assert succeeded is True
    assert "运行模式：Fake（临时 SQLite，不访问真实数据源）" in output
    assert "数据库迁移：已升级到当前 Alembic head" in output
    assert "第一进程：期初流水与买入流水已写入 SQLite" in output
    assert "第一次重启：持仓 12.00000000 份，流水 2 笔，数据仍然存在" in output
    assert "预计到账 15.92 CNY，FIFO 成本 12.00 CNY" in output
    assert "确认卖出：剩余 8.00000000 份，累计已实现收益 3.92 CNY" in output
    assert "第二次重启：剩余持仓 8.00000000 份，完整流水 3 笔" in output
    assert "临时数据库：验收结束后已自动删除" in output
    assert "真实网络请求：无" in output
    assert "个人数据库修改：无" in output
    assert "持久化与交易账本验收：通过" in output
    assert not personal_database.exists()


@pytest.mark.asyncio
async def test_script_reports_migration_failure(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alembic 迁移失败时必须返回失败，不能继续输出虚假的交易成功结果。"""

    def fail_upgrade(_database_path: Path) -> None:
        raise CommandError("测试迁移失败")

    monkeypatch.setattr(ledger_script, "_upgrade_database", fail_upgrade)

    succeeded = await ledger_script.check_persistence_ledger()

    output = capsys.readouterr().out
    assert succeeded is False
    assert "错误类型：CommandError" in output
    assert "测试迁移失败" in output
    assert "持久化与交易账本验收：失败" in output


def test_script_main_completes_with_temporary_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PyCharm 直接运行的同步 main 应以正常退出表示验收成功。"""

    ledger_script.main()

    assert "持久化与交易账本验收：通过" in capsys.readouterr().out
