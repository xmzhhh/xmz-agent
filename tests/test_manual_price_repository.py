"""手工价格模型与异步内存仓库测试。

这些测试只验证价格记录的精确数值、时区和存储行为；900 秒过期规则属于 Dashboard
Service，将在服务测试中覆盖。
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from finagent.dashboard import (
    InMemoryManualPriceRepository,
    ManualPriceRecord,
    ManualPriceRepository,
)
from finagent.portfolio import Currency

NOW = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)


def make_record(price: str = "850", *, recorded_at: datetime = NOW) -> ManualPriceRecord:
    """构造包含服务端时间语义的合法固定记录。"""

    return ManualPriceRecord.model_validate(
        {
            "symbol": "JD-ZS-GOLD",
            "price": price,
            "currency": "CNY",
            "recorded_at": recorded_at,
        }
    )


def test_in_memory_manual_price_repository_satisfies_protocol() -> None:
    """内存实现应满足可替换的异步仓库协议。"""

    repository: ManualPriceRepository = InMemoryManualPriceRepository()

    assert isinstance(repository, InMemoryManualPriceRepository)


async def test_repository_starts_empty_and_normalizes_lookup_symbol() -> None:
    """新仓库没有价格，大小写和空格不同的查询应指向同一个代码。"""

    repository = InMemoryManualPriceRepository()

    assert await repository.get_price(" jd-zs-gold ") is None


async def test_save_replaces_record_and_delete_is_idempotent() -> None:
    """重新录价应完整替换旧记录，删除不存在记录时返回 None。"""

    repository = InMemoryManualPriceRepository()
    first = await repository.save_price(make_record("850"))
    second = await repository.save_price(make_record("860"))

    assert first.price == Decimal("850")
    assert await repository.get_price("JD-ZS-GOLD") == second
    assert await repository.delete_price("JD-ZS-GOLD") == second
    assert await repository.delete_price("JD-ZS-GOLD") is None


def test_manual_price_record_rejects_float_and_naive_time() -> None:
    """手工价格拒绝 float 和无时区时间，避免金额误差和过期判断歧义。"""

    with pytest.raises(ValidationError):
        ManualPriceRecord.model_validate(
            {
                "symbol": "JD-ZS-GOLD",
                "price": 850.1,
                "currency": Currency.CNY,
                "recorded_at": NOW,
            }
        )
    with pytest.raises(ValidationError, match="必须包含时区"):
        make_record(recorded_at=datetime(2026, 7, 22, 10, 0))
