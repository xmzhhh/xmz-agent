"""供 Agent 查询真实资产状态的只读工具适配器。

这些工具只依赖 Dashboard 与交易账本应用服务的读取协议，不导入 SQLAlchemy Repository，
也不暴露创建、加仓、卖出、修改或删除方法。金融计算继续由已有 Service 和 Calculator 完成；
工具只负责校验模型参数、选择必要字段并转换为适合 Function Calling 的 JSON 数据。
"""

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import Field, field_validator

from finagent.dashboard import DashboardSnapshot
from finagent.ledger import LedgerTransaction, TransactionType
from finagent.portfolio import Holding
from finagent.portfolio.rounding import round_money
from finagent.tools.base import BaseTool, ToolInput


class PortfolioReadService(Protocol):
    """资产工具允许使用的最小 Dashboard 只读能力。"""

    async def get_dashboard(self) -> DashboardSnapshot:
        """返回包含确定性估值和行情来源的组合快照。"""

        ...

    async def get_holding(self, symbol: str) -> Holding:
        """按代码返回数据库中的当前持仓记录。"""

        ...


class LedgerReadService(Protocol):
    """账本工具允许使用的最小交易历史读取能力。"""

    async def list_transactions(
        self,
        symbol: str | None = None,
    ) -> tuple[LedgerTransaction, ...]:
        """返回全部资产或指定资产的不可变交易流水。"""

        ...


class PortfolioSnapshotInput(ToolInput):
    """组合快照不需要模型提供任何参数。"""


class PortfolioSnapshotTool(BaseTool[PortfolioSnapshotInput]):
    """读取当前组合估值、收益、仓位、集中度和数据质量。"""

    name = "get_portfolio_snapshot"
    description = (
        "读取用户当前投资组合的只读快照，包括各持仓当前估值、毛净收益、仓位权重、HHI、"
        "行情时间和来源。涉及用户当前资产状态时应调用本工具，不要根据旧对话猜测。"
        "工具不会新增、修改或交易任何资产。"
    )
    input_model = PortfolioSnapshotInput

    def __init__(self, service: PortfolioReadService) -> None:
        self._service = service

    async def run(self, tool_input: PortfolioSnapshotInput) -> dict[str, Any]:
        """复用 Dashboard Service 生成快照，并把 Decimal 序列化为字符串。"""

        del tool_input
        snapshot = await self._service.get_dashboard()
        return {
            "read_only": True,
            "snapshot": snapshot.model_dump(mode="json"),
        }


class HoldingRecordInput(ToolInput):
    """查询单项持仓时由模型提供的规范资产代码。"""

    symbol: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9._-]+$",
        description="持仓资产代码，例如 017811 或 JD-ZS-GOLD",
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> Any:
        """接受模型生成的小写代码，但在进入 Service 前统一成目录格式。"""

        return value.strip().upper() if isinstance(value, str) else value


class HoldingRecordTool(BaseTool[HoldingRecordInput]):
    """读取一项持仓的数量、成本和预计卖出费率记录。"""

    name = "get_holding_record"
    description = (
        "按资产代码查询 SQLite 中的当前持仓记录，包括数量、持仓均价和预计卖出费率。"
        "本工具不查询当前行情；需要当前价格、市值或浮盈亏时应调用 get_portfolio_snapshot。"
        "工具不会修改持仓。"
    )
    input_model = HoldingRecordInput

    def __init__(self, service: PortfolioReadService) -> None:
        self._service = service

    async def run(self, tool_input: HoldingRecordInput) -> dict[str, Any]:
        """读取规范持仓，并使用 JSON 模式保留 Decimal 字符串精度。"""

        holding = await self._service.get_holding(tool_input.symbol)
        return {
            "read_only": True,
            "holding": holding.model_dump(mode="json"),
        }


class TransactionLedgerSummaryInput(ToolInput):
    """账本摘要的可选资产过滤和最近流水数量。"""

    symbol: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9._-]+$",
        description="可选资产代码；不传表示汇总全部支持资产",
    )
    recent_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="返回最近流水的最大条数，范围 1 到 20",
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_optional_symbol(cls, value: Any) -> Any:
        """规范化可选代码；None 继续表示全部资产。"""

        return value.strip().upper() if isinstance(value, str) else value


class TransactionLedgerSummaryTool(BaseTool[TransactionLedgerSummaryInput]):
    """读取交易数量、现金流、已实现收益和有限条最近流水。"""

    name = "get_transaction_ledger_summary"
    description = (
        "只读查询模拟交易账本摘要，可按资产过滤；返回 opening、buy、sell 数量，买入实际支出、"
        "卖出实际到账、手续费、已实现收益和最近流水。不会试算或确认交易，也不会修改数据库。"
    )
    input_model = TransactionLedgerSummaryInput

    def __init__(self, service: LedgerReadService) -> None:
        self._service = service

    async def run(self, tool_input: TransactionLedgerSummaryInput) -> dict[str, Any]:
        """基于一次流水读取生成确定性摘要，避免查询期间出现前后不一致。"""

        transactions = await self._service.list_transactions(tool_input.symbol)
        counts = {
            transaction_type.value: sum(
                transaction.transaction_type is transaction_type
                for transaction in transactions
            )
            for transaction_type in TransactionType
        }
        buy_outflow = self._sum_amounts(
            transaction.cash_amount
            for transaction in transactions
            if transaction.transaction_type is TransactionType.BUY
        )
        sell_inflow = self._sum_amounts(
            transaction.cash_amount
            for transaction in transactions
            if transaction.transaction_type is TransactionType.SELL
        )
        total_fees = self._sum_amounts(
            transaction.fee_amount for transaction in transactions
        )
        realized_pnl = self._sum_amounts(
            transaction.realized_pnl
            for transaction in transactions
            if transaction.transaction_type is TransactionType.SELL
            and transaction.realized_pnl is not None
        )
        recent_transactions = tuple(
            self._public_transaction(transaction)
            for transaction in reversed(transactions[-tool_input.recent_limit :])
        )
        latest_occurred_at = transactions[-1].occurred_at if transactions else None
        return {
            "read_only": True,
            "symbol": tool_input.symbol,
            "currency": "CNY",
            "transaction_count": len(transactions),
            "transaction_counts": counts,
            "buy_cash_outflow": self._money_string(buy_outflow),
            "sell_cash_inflow": self._money_string(sell_inflow),
            "total_fees": self._money_string(total_fees),
            "realized_pnl": self._money_string(realized_pnl),
            "latest_occurred_at": self._datetime_string(latest_occurred_at),
            "recent_transactions": recent_transactions,
        }

    @staticmethod
    def _sum_amounts(values: Iterable[Decimal]) -> Decimal:
        """使用 Decimal 汇总金额，并复用账本的人民币两位舍入规则。"""

        return round_money(sum(values, start=Decimal("0")))

    @staticmethod
    def _money_string(value: Decimal) -> str:
        """金融金额固定输出两位十进制字符串，避免 JSON float 精度损失。"""

        return format(value, ".2f")

    @staticmethod
    def _datetime_string(value: datetime | None) -> str | None:
        """把带时区账本时间转换为标准 ISO 8601 字符串。"""

        return value.isoformat() if value is not None else None

    @staticmethod
    def _public_transaction(transaction: LedgerTransaction) -> dict[str, Any]:
        """返回模型完成研究所需字段，不发送 UUID、内部创建时间和自由文本备注。"""

        return {
            "symbol": transaction.symbol,
            "transaction_type": transaction.transaction_type.value,
            "quantity": str(transaction.quantity),
            "unit_price": str(transaction.unit_price),
            "gross_amount": format(transaction.gross_amount, ".2f"),
            "fee_amount": format(transaction.fee_amount, ".2f"),
            "cash_amount": format(transaction.cash_amount, ".2f"),
            "realized_pnl": (
                format(transaction.realized_pnl, ".2f")
                if transaction.realized_pnl is not None
                else None
            ),
            "currency": transaction.currency.value,
            "occurred_at": transaction.occurred_at.isoformat(),
        }
