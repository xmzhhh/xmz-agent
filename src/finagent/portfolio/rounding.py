"""金融数值的统一舍入规则。

金额和展示百分比不能在不同模块中随意调用 ``round``。本模块集中规定量化精度与
舍入方向，保证 CLI、API、测试和未来数据库中的同一笔数据得到一致结果。
"""

from decimal import ROUND_HALF_UP, Decimal

MONEY_QUANTUM = Decimal("0.01")
PERCENT_QUANTUM = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")
ZERO_PERCENT = Decimal("0.00")
ONE_HUNDRED_PERCENT = Decimal("100.00")


def round_money(value: Decimal) -> Decimal:
    """把金额按四舍五入规则保留两位小数。"""

    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def round_percent(value: Decimal) -> Decimal:
    """把百分比或 HHI 指标按四舍五入规则保留两位小数。"""

    return value.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
