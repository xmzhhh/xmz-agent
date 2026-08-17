"""投资组合的确定性估值与集中度计算引擎。

计算器接收已经通过 Pydantic 校验的持仓和行情，不访问网络、不调用大模型，也不读写
数据库。保持纯计算边界后，同一输入永远得到同一输出，方便单元测试、历史回放以及
未来把计算能力封装成 Agent 工具或 API。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from finagent.portfolio.errors import (
    CurrencyMismatchError,
    DuplicateHoldingError,
    DuplicateQuoteError,
    QuoteNotFoundError,
)
from finagent.portfolio.models import (
    AssetType,
    Currency,
    Holding,
    PortfolioSnapshot,
    Quote,
    ValuedHolding,
)
from finagent.portfolio.rounding import (
    ONE_HUNDRED_PERCENT,
    ZERO_MONEY,
    ZERO_PERCENT,
    round_money,
    round_percent,
)


@dataclass(frozen=True, slots=True)
class _PositionDraft:
    """尚未分配组合权重的内部估值结果。

    权重依赖组合总市值，必须在所有持仓完成估值后统一计算，因此先用内部不可变对象
    保存单项结果。下划线表示它不是 portfolio 包的公共接口。
    """

    holding: Holding
    quote: Quote
    cost_basis: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal
    return_percent: Decimal
    estimated_exit_fee: Decimal
    net_liquidation_value: Decimal
    net_unrealized_pnl: Decimal
    net_return_percent: Decimal


class PortfolioCalculator:
    """在单一基准币种下计算投资组合快照。

    Args:
        base_currency: 本次投资组合统一使用的币种。本阶段没有汇率模块，任何持仓或行情
            与该币种不一致时都会拒绝计算。
    """

    def __init__(self, base_currency: Currency) -> None:
        self._base_currency = base_currency

    def calculate(
        self,
        holdings: Sequence[Holding],
        quotes: Sequence[Quote],
    ) -> PortfolioSnapshot:
        """使用持仓与行情生成一次可复现的投资组合快照。

        Raises:
            DuplicateHoldingError: 持仓列表包含重复资产代码。
            DuplicateQuoteError: 行情列表包含重复资产代码。
            QuoteNotFoundError: 某项持仓没有对应行情。
            CurrencyMismatchError: 持仓、行情和基准币种不一致。
        """

        holding_items = tuple(holdings)
        quote_items = tuple(quotes)
        self._ensure_unique_holdings(holding_items)
        quote_by_symbol = self._index_quotes(quote_items)

        if not holding_items:
            return self._empty_snapshot()

        drafts = tuple(self._value_holding(holding, quote_by_symbol) for holding in holding_items)
        total_cost = sum((draft.cost_basis for draft in drafts), start=ZERO_MONEY)
        total_market_value = sum((draft.market_value for draft in drafts), start=ZERO_MONEY)
        total_unrealized_pnl = total_market_value - total_cost
        total_return_percent = round_percent(
            total_unrealized_pnl / total_cost * ONE_HUNDRED_PERCENT
        )
        # 汇总直接累加已经舍入到“分”的单项结果，确保网页明细相加与汇总卡片完全一致。
        total_estimated_exit_fee = sum(
            (draft.estimated_exit_fee for draft in drafts), start=ZERO_MONEY
        )
        total_net_liquidation_value = sum(
            (draft.net_liquidation_value for draft in drafts), start=ZERO_MONEY
        )
        total_net_unrealized_pnl = total_net_liquidation_value - total_cost
        total_net_return_percent = round_percent(
            total_net_unrealized_pnl / total_cost * ONE_HUNDRED_PERCENT
        )

        weights = self._calculate_display_weights(drafts, total_market_value)
        positions = tuple(
            self._build_valued_holding(draft, weights[index]) for index, draft in enumerate(drafts)
        )
        asset_type_weights = self._group_asset_type_weights(positions)
        max_index = max(range(len(drafts)), key=lambda index: drafts[index].market_value)
        concentration_hhi = round_percent(
            sum((weight * weight for weight in weights), start=ZERO_PERCENT)
        )

        return PortfolioSnapshot(
            base_currency=self._base_currency,
            positions=positions,
            total_cost=total_cost,
            total_market_value=total_market_value,
            total_unrealized_pnl=total_unrealized_pnl,
            total_return_percent=total_return_percent,
            total_estimated_exit_fee=total_estimated_exit_fee,
            total_net_liquidation_value=total_net_liquidation_value,
            total_net_unrealized_pnl=total_net_unrealized_pnl,
            total_net_return_percent=total_net_return_percent,
            asset_type_weights=asset_type_weights,
            max_position_symbol=drafts[max_index].holding.symbol,
            max_position_weight_percent=weights[max_index],
            concentration_hhi=concentration_hhi,
            # 组合只能声称“截至最旧行情时间”有效，不能用最新一条行情掩盖其他旧数据。
            as_of=min(draft.quote.as_of for draft in drafts),
            has_delayed_data=any(draft.quote.is_delayed for draft in drafts),
        )

    def _value_holding(
        self,
        holding: Holding,
        quote_by_symbol: dict[str, Quote],
    ) -> _PositionDraft:
        """匹配一项持仓与行情，并计算舍入后的金额及收益率。"""

        if holding.currency != self._base_currency:
            raise CurrencyMismatchError(
                f"持仓 {holding.symbol} 的币种 {holding.currency} "
                f"与基准币种 {self._base_currency} 不一致"
            )

        try:
            quote = quote_by_symbol[holding.symbol]
        except KeyError as error:
            raise QuoteNotFoundError(f"持仓 {holding.symbol} 缺少对应行情") from error

        if quote.currency != holding.currency:
            raise CurrencyMismatchError(
                f"持仓 {holding.symbol} 使用 {holding.currency}，但行情使用 {quote.currency}"
            )

        # 先把展示金额量化到分，再从量化金额计算盈亏，保证明细相加与汇总完全一致。
        cost_basis = round_money(holding.quantity * holding.average_cost)
        market_value = round_money(holding.quantity * quote.price)
        unrealized_pnl = market_value - cost_basis
        return_percent = round_percent(unrealized_pnl / cost_basis * ONE_HUNDRED_PERCENT)

        # 持仓费率使用百分数语义，所以必须除以 100。费用也先量化到分，避免组合汇总与
        # 页面逐项展示产生一分钱差异。费率最多为 100%，因此净到账金额不会小于零。
        estimated_exit_fee = round_money(
            market_value * holding.estimated_exit_fee_percent / ONE_HUNDRED_PERCENT
        )
        net_liquidation_value = market_value - estimated_exit_fee
        net_unrealized_pnl = net_liquidation_value - cost_basis
        net_return_percent = round_percent(net_unrealized_pnl / cost_basis * ONE_HUNDRED_PERCENT)
        return _PositionDraft(
            holding=holding,
            quote=quote,
            cost_basis=cost_basis,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            return_percent=return_percent,
            estimated_exit_fee=estimated_exit_fee,
            net_liquidation_value=net_liquidation_value,
            net_unrealized_pnl=net_unrealized_pnl,
            net_return_percent=net_return_percent,
        )

    @staticmethod
    def _calculate_display_weights(
        drafts: tuple[_PositionDraft, ...],
        total_market_value: Decimal,
    ) -> tuple[Decimal, ...]:
        """计算两位小数权重，并修正舍入差使总和严格等于 100.00%。

        每项独立四舍五入后，权重和可能是 99.99% 或 100.01%。这里把最多几分钱比例的
        差额调整到市值最大的持仓，既保持确定性，也避免很小的仓位被修正成负数。
        """

        weights = [
            round_percent(draft.market_value / total_market_value * ONE_HUNDRED_PERCENT)
            for draft in drafts
        ]
        difference = ONE_HUNDRED_PERCENT - sum(weights, start=ZERO_PERCENT)
        largest_index = max(range(len(drafts)), key=lambda index: drafts[index].market_value)
        weights[largest_index] = round_percent(weights[largest_index] + difference)
        return tuple(weights)

    @staticmethod
    def _build_valued_holding(
        draft: _PositionDraft,
        weight_percent: Decimal,
    ) -> ValuedHolding:
        """把内部估值草稿转换成稳定的公共领域模型。"""

        return ValuedHolding(
            symbol=draft.holding.symbol,
            name=draft.holding.name,
            asset_type=draft.holding.asset_type,
            currency=draft.holding.currency,
            quantity=draft.holding.quantity,
            average_cost=draft.holding.average_cost,
            current_price=draft.quote.price,
            cost_basis=draft.cost_basis,
            market_value=draft.market_value,
            unrealized_pnl=draft.unrealized_pnl,
            return_percent=draft.return_percent,
            estimated_exit_fee_percent=draft.holding.estimated_exit_fee_percent,
            estimated_exit_fee=draft.estimated_exit_fee,
            net_liquidation_value=draft.net_liquidation_value,
            net_unrealized_pnl=draft.net_unrealized_pnl,
            net_return_percent=draft.net_return_percent,
            weight_percent=weight_percent,
            quote_as_of=draft.quote.as_of,
            quote_source=draft.quote.source,
            quote_is_delayed=draft.quote.is_delayed,
        )

    @staticmethod
    def _group_asset_type_weights(
        positions: tuple[ValuedHolding, ...],
    ) -> dict[AssetType, Decimal]:
        """把持仓权重按资产类别汇总，供风险展示与后续规则判断。"""

        result: dict[AssetType, Decimal] = {}
        for position in positions:
            result[position.asset_type] = (
                result.get(position.asset_type, ZERO_PERCENT) + position.weight_percent
            )
        return result

    @staticmethod
    def _ensure_unique_holdings(holdings: tuple[Holding, ...]) -> None:
        """拒绝重复持仓，避免同一资产被重复计入总市值。"""

        seen: set[str] = set()
        for holding in holdings:
            if holding.symbol in seen:
                raise DuplicateHoldingError(f"持仓代码重复：{holding.symbol}")
            seen.add(holding.symbol)

    @staticmethod
    def _index_quotes(quotes: tuple[Quote, ...]) -> dict[str, Quote]:
        """建立行情索引，并拒绝同一代码出现多条歧义行情。"""

        result: dict[str, Quote] = {}
        for quote in quotes:
            if quote.symbol in result:
                raise DuplicateQuoteError(f"行情代码重复：{quote.symbol}")
            result[quote.symbol] = quote
        return result

    def _empty_snapshot(self) -> PortfolioSnapshot:
        """返回字段自洽的空投资组合快照。"""

        return PortfolioSnapshot(
            base_currency=self._base_currency,
            total_cost=ZERO_MONEY,
            total_market_value=ZERO_MONEY,
            total_unrealized_pnl=ZERO_MONEY,
            total_return_percent=None,
            total_estimated_exit_fee=ZERO_MONEY,
            total_net_liquidation_value=ZERO_MONEY,
            total_net_unrealized_pnl=ZERO_MONEY,
            total_net_return_percent=None,
            asset_type_weights={},
            max_position_symbol=None,
            max_position_weight_percent=ZERO_PERCENT,
            concentration_hhi=ZERO_PERCENT,
            as_of=None,
            has_delayed_data=False,
        )
