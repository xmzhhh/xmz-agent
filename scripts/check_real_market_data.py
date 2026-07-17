"""在 PyCharm 中人工验收 AKShare 与 GoldAPI 的真实数据连接。

脚本默认查询东方人工智能主题混合C（017811）的最新确认单位净值，以及 GoldAPI 的
``XAU-CNY-GRAM`` 国际 24K 黄金人民币克价。它会产生真实网络请求，GoldAPI 调用会消耗
免费额度，因此不能放入 pytest 日常自动测试。

可测试的检查流程位于 ``finagent.data.diagnostics``；本文件只处理命令行参数、配置加载、
事件循环和退出码，保持人工入口与业务编排分离。
"""

import argparse
import asyncio

from pydantic import ValidationError

from finagent.core.config import get_settings
from finagent.data.diagnostics import DEFAULT_FUND_CODE, check_real_market_data


def _parse_arguments() -> argparse.Namespace:
    """解析人工验收选项；PyCharm 直接运行时默认同时检查两个数据源。"""

    parser = argparse.ArgumentParser(description="验证 FinAgent 的真实基金与黄金行情配置")
    parser.add_argument(
        "--fund-code",
        default=DEFAULT_FUND_CODE,
        help="待查询的六位开放式基金代码，默认 017811",
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--only-fund", action="store_true", help="只检查 AKShare 基金净值")
    source_group.add_argument("--only-gold", action="store_true", help="只检查 GoldAPI 黄金参考价")
    return parser.parse_args()


def main() -> None:
    """加载项目根目录配置，运行异步检查，并通过进程退出码表达验收结果。"""

    args = _parse_arguments()
    try:
        settings = get_settings()
    except ValidationError:
        # 不回显完整配置验证对象，避免未来第三方错误消息意外包含敏感输入。
        print("配置加载失败：请检查项目根目录 .env 中的必填项和数据类型。")
        raise SystemExit(1) from None

    succeeded = asyncio.run(
        check_real_market_data(
            settings,
            fund_code=args.fund_code,
            include_fund=not args.only_gold,
            include_gold=not args.only_fund,
        )
    )
    if not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
