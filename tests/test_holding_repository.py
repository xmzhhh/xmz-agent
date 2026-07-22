"""异步内存持仓仓库的 CRUD、目录约束和原子载入测试。

测试使用全新仓库实例，不访问网络或真实持仓。每个场景都验证仓库边界，避免未来接入
FastAPI 后出现重复资产、半批演示数据或静默修改资产代码。
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from finagent.portfolio import (
    AssetNotHoldableError,
    AssetType,
    DemoPortfolioConflictError,
    DuplicateHoldingError,
    HoldingCreate,
    HoldingNotFoundError,
    HoldingRepository,
    HoldingUpdate,
    InMemoryHoldingRepository,
    UnsupportedAssetError,
)


def make_create(
    symbol: str = "017811",
    *,
    quantity: str = "10",
    average_cost: str = "3.50",
    estimated_exit_fee_percent: str = "0.5",
) -> HoldingCreate:
    """通过公开输入模型构造可复用的测试持仓命令。"""

    return HoldingCreate.model_validate(
        {
            "symbol": symbol,
            "quantity": quantity,
            "average_cost": average_cost,
            "estimated_exit_fee_percent": estimated_exit_fee_percent,
        }
    )


def make_update(
    *,
    quantity: str = "20",
    average_cost: str = "3.60",
    estimated_exit_fee_percent: str = "0.25",
) -> HoldingUpdate:
    """构造不包含资产代码的完整更新命令。"""

    return HoldingUpdate.model_validate(
        {
            "quantity": quantity,
            "average_cost": average_cost,
            "estimated_exit_fee_percent": estimated_exit_fee_percent,
        }
    )


async def test_repository_starts_empty() -> None:
    """每个内存仓库实例启动时都应为空，不能携带上次进程的真实持仓。"""

    repository = InMemoryHoldingRepository()

    assert await repository.list_holdings() == ()


async def test_new_repository_instance_does_not_reuse_previous_holdings() -> None:
    """新进程对应的新仓库实例应为空，明确当前阶段没有持久化能力。"""

    first_repository = InMemoryHoldingRepository()
    await first_repository.create_holding(make_create())

    restarted_repository = InMemoryHoldingRepository()

    assert await restarted_repository.list_holdings() == ()


def test_in_memory_repository_satisfies_repository_protocol() -> None:
    """内存实现应能替换为仓库协议类型，保证 Dashboard Service 不依赖内部字典。"""

    repository: HoldingRepository = InMemoryHoldingRepository()

    assert isinstance(repository, InMemoryHoldingRepository)


async def test_create_uses_catalog_metadata_instead_of_user_metadata() -> None:
    """用户只提交数量、均价和费率，名称、类型与币种必须来自资产目录。"""

    repository = InMemoryHoldingRepository()
    holding = await repository.create_holding(make_create(symbol=" 017811 "))

    assert holding.symbol == "017811"
    assert holding.name == "东方人工智能主题混合C"
    assert holding.asset_type is AssetType.FUND
    assert holding.quantity == Decimal("10")
    assert holding.estimated_exit_fee_percent == Decimal("0.5")


def test_create_input_rejects_user_supplied_catalog_metadata() -> None:
    """创建表单不能偷偷覆盖目录维护的名称、类型或币种。"""

    with pytest.raises(ValidationError, match="extra_forbidden"):
        HoldingCreate.model_validate(
            {
                "symbol": "017811",
                "name": "伪造名称",
                "quantity": "10",
                "average_cost": "3.50",
            }
        )


async def test_create_rejects_duplicate_holding() -> None:
    """同一个资产只能存在一条持仓，防止组合市值被重复计算。"""

    repository = InMemoryHoldingRepository()
    await repository.create_holding(make_create())

    with pytest.raises(DuplicateHoldingError, match="017811"):
        await repository.create_holding(make_create(quantity="99"))


async def test_create_rejects_unknown_and_reference_only_assets() -> None:
    """未知代码和国际黄金参考代码都不能创建为持仓，但应使用不同异常解释原因。"""

    repository = InMemoryHoldingRepository()

    with pytest.raises(UnsupportedAssetError, match="UNKNOWN"):
        await repository.create_holding(make_create("UNKNOWN"))
    with pytest.raises(AssetNotHoldableError, match="XAU-CNY-GRAM"):
        await repository.create_holding(make_create("XAU-CNY-GRAM"))


async def test_update_changes_values_but_preserves_identity_metadata() -> None:
    """更新只能改变数值，资产代码、名称、类型和币种必须保持不变。"""

    repository = InMemoryHoldingRepository()
    original = await repository.create_holding(make_create())
    updated = await repository.update_holding("017811", make_update())

    assert updated.symbol == original.symbol
    assert updated.name == original.name
    assert updated.asset_type is original.asset_type
    assert updated.currency is original.currency
    assert updated.quantity == Decimal("20")
    assert updated.average_cost == Decimal("3.60")
    assert updated.estimated_exit_fee_percent == Decimal("0.25")


async def test_update_rejects_missing_holding() -> None:
    """更新不存在的代码不能被误解为创建操作，应明确交给上层映射为 404。"""

    repository = InMemoryHoldingRepository()

    with pytest.raises(HoldingNotFoundError, match="017811"):
        await repository.update_holding("017811", make_update())


def test_update_input_rejects_symbol_change() -> None:
    """更新模型没有 symbol 字段，换资产必须通过删除后重新创建完成。"""

    with pytest.raises(ValidationError, match="extra_forbidden"):
        HoldingUpdate.model_validate(
            {
                "symbol": "JD-ZS-GOLD",
                "quantity": "20",
                "average_cost": "3.60",
            }
        )


async def test_list_holdings_is_sorted_by_symbol() -> None:
    """列表顺序不能依赖创建先后，保证 API、网页和测试输出可复现。"""

    repository = InMemoryHoldingRepository()
    await repository.create_holding(make_create("JD-ZS-GOLD", average_cost="800"))
    await repository.create_holding(make_create("017811"))

    holdings = await repository.list_holdings()

    assert [holding.symbol for holding in holdings] == ["017811", "JD-ZS-GOLD"]


async def test_get_and_delete_raise_not_found_after_removal() -> None:
    """删除应返回被删持仓，随后读取和再次删除都应明确报告不存在。"""

    repository = InMemoryHoldingRepository()
    created = await repository.create_holding(make_create())

    assert await repository.get_holding(" 017811 ") == created
    assert await repository.delete_holding("017811") == created
    with pytest.raises(HoldingNotFoundError, match="017811"):
        await repository.get_holding("017811")
    with pytest.raises(HoldingNotFoundError, match="017811"):
        await repository.delete_holding("017811")


async def test_load_demo_is_atomic_and_returns_sorted_holdings() -> None:
    """空仓库应一次性载入完整演示组合，并返回确定性顺序。"""

    repository = InMemoryHoldingRepository()

    holdings = await repository.load_demo(
        (make_create("JD-ZS-GOLD", average_cost="800"), make_create("017811"))
    )

    assert [holding.symbol for holding in holdings] == ["017811", "JD-ZS-GOLD"]
    assert await repository.list_holdings() == holdings


async def test_load_demo_rejects_non_empty_repository_without_merging() -> None:
    """已有持仓时演示载入必须冲突失败，不能覆盖或追加任何数据。"""

    repository = InMemoryHoldingRepository()
    original = await repository.create_holding(make_create())

    with pytest.raises(DemoPortfolioConflictError, match="已有持仓"):
        await repository.load_demo((make_create("JD-ZS-GOLD", average_cost="800"),))

    assert await repository.list_holdings() == (original,)


async def test_load_demo_validation_failure_leaves_repository_empty() -> None:
    """批次中任一资产不合法时不能留下前半批数据，保证演示载入原子性。"""

    repository = InMemoryHoldingRepository()

    with pytest.raises(UnsupportedAssetError, match="UNKNOWN"):
        await repository.load_demo((make_create("017811"), make_create("UNKNOWN")))

    assert await repository.list_holdings() == ()


async def test_load_demo_rejects_duplicate_input_without_partial_write() -> None:
    """演示批次内部重复也必须整批失败，并保持仓库为空。"""

    repository = InMemoryHoldingRepository()

    with pytest.raises(DuplicateHoldingError, match="017811"):
        await repository.load_demo((make_create("017811"), make_create("017811")))

    assert await repository.list_holdings() == ()
