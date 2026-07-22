"""Phase 6 资产目录的白名单与估值方式测试。

这些测试保证用户不能自行伪造资产名称、类型或估值来源，也防止国际黄金参考代码被错误
录入为真实持仓。
"""

import pytest

from finagent.portfolio import (
    DEFAULT_ASSET_CATALOG,
    AssetCatalog,
    AssetDefinition,
    AssetNotHoldableError,
    AssetType,
    AssetValuationMethod,
    Currency,
    UnsupportedAssetError,
)


def test_default_catalog_contains_supported_assets_in_deterministic_order() -> None:
    """默认目录应同时包含两个持仓资产和一个参考资产，并按代码稳定排序。"""

    assets = DEFAULT_ASSET_CATALOG.list_assets()

    assert [asset.symbol for asset in assets] == ["017811", "JD-ZS-GOLD", "XAU-CNY-GRAM"]


def test_catalog_provides_canonical_fund_metadata() -> None:
    """基金代码应由目录补全规范名称、基金类型、人民币和自动行情估值方式。"""

    asset = DEFAULT_ASSET_CATALOG.get(" 017811 ")

    assert asset.name == "东方人工智能主题混合C"
    assert asset.asset_type is AssetType.FUND
    assert asset.currency is Currency.CNY
    assert asset.valuation_method is AssetValuationMethod.MARKET_DATA
    assert asset.is_holding_supported is True


def test_catalog_marks_jd_gold_as_manual_price_holding() -> None:
    """京东积存金允许录入持仓，但必须等待用户提供可成交卖出价。"""

    asset = DEFAULT_ASSET_CATALOG.require_holding_asset("jd-zs-gold")

    assert asset.name == "京东金融浙商银行积存金"
    assert asset.asset_type is AssetType.GOLD
    assert asset.valuation_method is AssetValuationMethod.MANUAL_PRICE


def test_catalog_rejects_unknown_asset() -> None:
    """目录外代码不能依靠格式猜测资产类型，应明确报告当前不支持。"""

    with pytest.raises(UnsupportedAssetError, match="UNKNOWN"):
        DEFAULT_ASSET_CATALOG.get("unknown")


def test_reference_gold_cannot_be_used_as_holding() -> None:
    """国际黄金只用于对比，不能替代京东可成交卖价进入持仓。"""

    with pytest.raises(AssetNotHoldableError, match="仅供参考"):
        DEFAULT_ASSET_CATALOG.require_holding_asset("XAU-CNY-GRAM")


def test_catalog_rejects_duplicate_symbols_during_initialization() -> None:
    """重复目录代码会造成路由歧义，应在应用启动时立即失败。"""

    asset = AssetDefinition(
        symbol="TEST",
        name="测试资产",
        asset_type=AssetType.OTHER,
        currency=Currency.CNY,
        valuation_method=AssetValuationMethod.MARKET_DATA,
        is_holding_supported=True,
    )

    with pytest.raises(ValueError, match="代码重复"):
        AssetCatalog((asset, asset))
