"""Phase 6 支持的资产目录与估值方式。

资产目录是用户输入与内部领域模型之间的白名单。它统一保存资产代码、规范名称、类型、
币种和估值方式，避免前端或用户自行声明这些关键元数据。目录只描述“支持什么”，不负责
读取行情、保存持仓或执行收益计算。
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from finagent.portfolio.errors import AssetNotHoldableError, UnsupportedAssetError
from finagent.portfolio.models import AssetType, Currency, FinancialModel


class AssetValuationMethod(StrEnum):
    """资产面板取得估值价格的方式。"""

    MARKET_DATA = "market_data"
    MANUAL_PRICE = "manual_price"
    REFERENCE_ONLY = "reference_only"


class AssetDefinition(FinancialModel):
    """一项受支持资产的规范元数据。"""

    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9._-]+$")
    name: str = Field(min_length=1, max_length=100)
    asset_type: AssetType
    currency: Currency
    valuation_method: AssetValuationMethod
    is_holding_supported: bool

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> Any:
        """目录中的代码也使用大写规范，防止初始化时产生不可达条目。"""

        return value.strip().upper() if isinstance(value, str) else value


def normalize_asset_symbol(symbol: str) -> str:
    """把查询路径或仓库方法收到的代码统一为目录键格式。"""

    return symbol.strip().upper()


class AssetCatalog:
    """提供确定性查询顺序的只读资产目录。

    Args:
        assets: 目录条目。代码必须唯一；初始化完成后，调用方无法取得内部可变字典。

    Raises:
        ValueError: 初始化数据包含重复代码。
    """

    def __init__(self, assets: Sequence[AssetDefinition]) -> None:
        asset_by_symbol: dict[str, AssetDefinition] = {}
        for asset in assets:
            if asset.symbol in asset_by_symbol:
                raise ValueError(f"资产目录代码重复：{asset.symbol}")
            asset_by_symbol[asset.symbol] = asset
        self._asset_by_symbol = asset_by_symbol

    def list_assets(self) -> tuple[AssetDefinition, ...]:
        """按代码排序返回不可变目录，保证 API 和测试输出顺序稳定。"""

        return tuple(self._asset_by_symbol[symbol] for symbol in sorted(self._asset_by_symbol))

    def get(self, symbol: str) -> AssetDefinition:
        """查询目录条目，未知代码使用明确领域异常失败。"""

        normalized_symbol = normalize_asset_symbol(symbol)
        try:
            return self._asset_by_symbol[normalized_symbol]
        except KeyError as error:
            raise UnsupportedAssetError(f"不支持的资产代码：{normalized_symbol}") from error

    def require_holding_asset(self, symbol: str) -> AssetDefinition:
        """返回允许录入持仓的目录条目，拒绝仅供参考的资产。"""

        asset = self.get(symbol)
        if not asset.is_holding_supported:
            raise AssetNotHoldableError(f"资产 {asset.symbol} 仅供参考，不能录入为持仓")
        return asset


DEFAULT_ASSET_CATALOG = AssetCatalog(
    (
        AssetDefinition(
            symbol="017811",
            name="东方人工智能主题混合C",
            asset_type=AssetType.FUND,
            currency=Currency.CNY,
            valuation_method=AssetValuationMethod.MARKET_DATA,
            is_holding_supported=True,
        ),
        AssetDefinition(
            symbol="JD-ZS-GOLD",
            name="京东金融浙商银行积存金",
            asset_type=AssetType.GOLD,
            currency=Currency.CNY,
            valuation_method=AssetValuationMethod.MANUAL_PRICE,
            is_holding_supported=True,
        ),
        AssetDefinition(
            symbol="XAU-CNY-GRAM",
            name="GoldAPI 国际黄金人民币克价参考",
            asset_type=AssetType.GOLD,
            currency=Currency.CNY,
            valuation_method=AssetValuationMethod.REFERENCE_ONLY,
            is_holding_supported=False,
        ),
    )
)
