"""创建 Phase 7 持仓、手工价格、交易流水和买入批次表。

Revision ID: 20260817_01
Revises:
Create Date: 2026-08-17 15:55:29.736316
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建首版持久化表、约束和查询索引。"""

    op.create_table(
        "holding_positions",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("average_cost", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column(
            "estimated_exit_fee_percent",
            sa.Numeric(precision=9, scale=6),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "currency IN ('CNY', 'USD', 'HKD')",
            name=op.f("ck_holding_positions_currency_supported"),
        ),
        sa.CheckConstraint(
            "average_cost > 0",
            name=op.f("ck_holding_positions_average_cost_positive"),
        ),
        sa.CheckConstraint(
            "estimated_exit_fee_percent BETWEEN 0 AND 100",
            name=op.f("ck_holding_positions_exit_fee_percent_range"),
        ),
        sa.CheckConstraint(
            "length(symbol) BETWEEN 1 AND 32",
            name=op.f("ck_holding_positions_symbol_length"),
        ),
        sa.CheckConstraint(
            "quantity > 0",
            name=op.f("ck_holding_positions_quantity_positive"),
        ),
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_holding_positions")),
    )
    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("transaction_type", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("fee_amount", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("cash_amount", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "currency IN ('CNY', 'USD', 'HKD')",
            name=op.f("ck_ledger_transactions_currency_supported"),
        ),
        sa.CheckConstraint(
            "transaction_type IN ('opening', 'buy', 'sell', 'adjustment')",
            name=op.f("ck_ledger_transactions_transaction_type_supported"),
        ),
        sa.CheckConstraint(
            "cash_amount >= 0",
            name=op.f("ck_ledger_transactions_cash_amount_non_negative"),
        ),
        sa.CheckConstraint(
            "fee_amount >= 0",
            name=op.f("ck_ledger_transactions_fee_amount_non_negative"),
        ),
        sa.CheckConstraint(
            "gross_amount > 0",
            name=op.f("ck_ledger_transactions_gross_amount_positive"),
        ),
        sa.CheckConstraint(
            "length(symbol) BETWEEN 1 AND 32",
            name=op.f("ck_ledger_transactions_symbol_length"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(note) <= 500",
            name=op.f("ck_ledger_transactions_note_length"),
        ),
        sa.CheckConstraint(
            "quantity > 0",
            name=op.f("ck_ledger_transactions_quantity_positive"),
        ),
        sa.CheckConstraint(
            "unit_price > 0",
            name=op.f("ck_ledger_transactions_unit_price_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_transactions")),
    )
    with op.batch_alter_table("ledger_transactions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_ledger_transactions_symbol_occurred_at",
            ["symbol", "occurred_at"],
            unique=False,
        )

    op.create_table(
        "manual_prices",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("price", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "currency IN ('CNY', 'USD', 'HKD')",
            name=op.f("ck_manual_prices_currency_supported"),
        ),
        sa.CheckConstraint(
            "length(symbol) BETWEEN 1 AND 32",
            name=op.f("ck_manual_prices_symbol_length"),
        ),
        sa.CheckConstraint(
            "price > 0",
            name=op.f("ck_manual_prices_price_positive"),
        ),
        sa.PrimaryKeyConstraint("symbol", name=op.f("pk_manual_prices")),
    )
    op.create_table(
        "purchase_lots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opening_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("original_quantity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("remaining_quantity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(symbol) BETWEEN 1 AND 32",
            name=op.f("ck_purchase_lots_symbol_length"),
        ),
        sa.CheckConstraint(
            "original_quantity > 0",
            name=op.f("ck_purchase_lots_original_quantity_positive"),
        ),
        sa.CheckConstraint(
            "remaining_quantity <= original_quantity",
            name=op.f("ck_purchase_lots_remaining_not_above_original"),
        ),
        sa.CheckConstraint(
            "remaining_quantity >= 0",
            name=op.f("ck_purchase_lots_remaining_quantity_non_negative"),
        ),
        sa.CheckConstraint(
            "unit_cost > 0",
            name=op.f("ck_purchase_lots_unit_cost_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["opening_transaction_id"],
            ["ledger_transactions.id"],
            name=op.f("fk_purchase_lots_opening_transaction_id_ledger_transactions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase_lots")),
        sa.UniqueConstraint(
            "opening_transaction_id",
            name=op.f("uq_purchase_lots_opening_transaction_id"),
        ),
    )
    with op.batch_alter_table("purchase_lots", schema=None) as batch_op:
        batch_op.create_index(
            "ix_purchase_lots_symbol_acquired_at",
            ["symbol", "acquired_at"],
            unique=False,
        )


def downgrade() -> None:
    """按外键依赖的逆序删除本版本创建的表。"""

    with op.batch_alter_table("purchase_lots", schema=None) as batch_op:
        batch_op.drop_index("ix_purchase_lots_symbol_acquired_at")

    op.drop_table("purchase_lots")
    op.drop_table("manual_prices")
    with op.batch_alter_table("ledger_transactions", schema=None) as batch_op:
        batch_op.drop_index("ix_ledger_transactions_symbol_occurred_at")

    op.drop_table("ledger_transactions")
    op.drop_table("holding_positions")
