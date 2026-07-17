"""真实市场数据源的人工联调编排与安全输出。

本模块组织 AKShare 基金净值和 GoldAPI 国际黄金参考价的人工验收，但不包含命令行参数
解析。把可测试流程放在 ``src/finagent`` 中，PyCharm 脚本只负责调用，避免测试依赖项目
根目录是否恰好出现在 ``sys.path``。

两个数据源分别执行和报告：一个来源出现可预期的配置、网络或响应错误时，另一个来源仍会
继续检查。输出只使用已经校验的统一 ``Quote``，不会访问或打印任何 API Key。
"""

from dataclasses import dataclass

from finagent.core.config import Settings
from finagent.data.akshare import AkShareFundNavProvider
from finagent.data.base import MarketDataProvider
from finagent.data.errors import MarketDataError
from finagent.data.goldapi import GOLD_REFERENCE_SYMBOL, GoldApiMarketDataProvider
from finagent.data.service import MarketDataService
from finagent.portfolio import Quote

DEFAULT_FUND_CODE = "017811"

_FUND_REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class _CheckTarget:
    """描述一次人工行情检查的显示语义和请求参数。

    Attributes:
        title: 控制台中展示的数据源名称。
        symbol: 交给统一市场数据服务的资产代码。
        price_unit: 价格数值对应的单位，防止只打印数字造成误解。
        note: 面向验收者的业务边界说明。
    """

    title: str
    symbol: str
    price_unit: str
    note: str


def _print_quote(target: _CheckTarget, quote: Quote) -> None:
    """以固定格式打印已经校验的统一行情，不接触供应商原始响应。

    Args:
        target: 当前检查项的标题、单位和业务说明。
        quote: Provider 已完成字段和金融数值校验的统一行情。
    """

    delayed_text = "是" if quote.is_delayed else "否"
    print(f"\n[成功] {target.title}")
    print(f"资产代码：{quote.symbol}")
    print(f"价格：{quote.price} {target.price_unit}")
    print(f"币种：{quote.currency}")
    print(f"数据时间：{quote.as_of.isoformat()}")
    print(f"数据来源：{quote.source}")
    print(f"延迟数据：{delayed_text}")
    print(f"业务说明：{target.note}")


async def _check_one_source(
    target: _CheckTarget,
    provider: MarketDataProvider,
    *,
    request_timeout_seconds: float,
) -> bool:
    """检查一个真实数据源并保证其资源最终被关闭。

    Args:
        target: 本次请求的资产代码和显示语义。
        provider: 已构造的真实或测试 Provider；本函数接管其生命周期。
        request_timeout_seconds: 应用层允许的最长等待秒数。

    Returns:
        查询成功返回 ``True``；可预期的配置、网络或响应错误返回 ``False``。

    Notes:
        这里只捕获项目已经分类的市场数据异常和输入 ``ValueError``。编程错误仍会向外抛出，
        避免验收流程把真实代码缺陷伪装成普通网络失败。
    """

    service = MarketDataService(
        provider,
        request_timeout_seconds=request_timeout_seconds,
    )
    try:
        quote = await service.get_quote(target.symbol)
    except (MarketDataError, ValueError) as error:
        print(f"\n[失败] {target.title}")
        print(f"错误类型：{type(error).__name__}")
        print(f"错误信息：{error}")
        return False
    finally:
        # 即使请求超时、鉴权失败或响应字段错误，也必须释放 HTTP 连接并清空缓存。
        await service.close()

    _print_quote(target, quote)
    return True


async def check_real_market_data(
    settings: Settings,
    *,
    fund_code: str = DEFAULT_FUND_CODE,
    include_fund: bool = True,
    include_gold: bool = True,
    fund_provider: MarketDataProvider | None = None,
    gold_provider: MarketDataProvider | None = None,
) -> bool:
    """按独立步骤检查基金净值和国际黄金参考价。

    Args:
        settings: 已校验应用配置；构造真实 GoldAPI Provider 时从中读取密钥和超时。
        fund_code: 待检查的六位开放式基金代码，默认 ``017811``。
        include_fund: 是否执行 AKShare 基金检查。
        include_gold: 是否执行 GoldAPI 黄金检查。
        fund_provider: 可选注入 Provider，供离线测试替代真实 AKShare。
        gold_provider: 可选注入 Provider，供离线测试替代真实 GoldAPI。

    Returns:
        所有已选择检查项均成功时返回 ``True``；任意一项失败返回 ``False``。

    Raises:
        ValueError: 两个检查项都被关闭时抛出，避免流程什么也不做却报告成功。
    """

    if not include_fund and not include_gold:
        raise ValueError("至少需要选择一个真实数据源进行检查")

    results: list[bool] = []

    if include_fund:
        fund_target = _CheckTarget(
            title=f"AKShare 基金 {fund_code} 最新确认净值",
            symbol=fund_code,
            price_unit="人民币/份",
            note="场外基金按每日确认净值估值，不是盘中实时成交价。",
        )
        actual_fund_provider = fund_provider or AkShareFundNavProvider()
        results.append(
            await _check_one_source(
                fund_target,
                actual_fund_provider,
                request_timeout_seconds=_FUND_REQUEST_TIMEOUT_SECONDS,
            )
        )

    if include_gold:
        gold_target = _CheckTarget(
            title="GoldAPI 国际黄金人民币克价",
            symbol=GOLD_REFERENCE_SYMBOL,
            price_unit="人民币/克",
            note="这是国际24K黄金参考价，不是京东积存金可成交卖出价。",
        )
        try:
            # 测试注入假 Provider 时不读取 GoldAPI 密钥；真实运行时才构造 HTTP 实现。
            actual_gold_provider = gold_provider or GoldApiMarketDataProvider(settings)
        except ValueError as error:
            print(f"\n[失败] {gold_target.title}")
            print(f"错误类型：{type(error).__name__}")
            print(f"错误信息：{error}")
            results.append(False)
        else:
            results.append(
                await _check_one_source(
                    gold_target,
                    actual_gold_provider,
                    # HTTP 客户端自身先按配置超时；应用层多留 1 秒用于响应读取和异常转换。
                    request_timeout_seconds=settings.goldapi_timeout_seconds + 1,
                )
            )

    all_succeeded = all(results)
    print("\n=== 验收汇总 ===")
    print(f"检查项：{len(results)}，全部成功：{'是' if all_succeeded else '否'}")
    print("安全提示：输出中不应出现 LLM_API_KEY 或 GOLDAPI_API_KEY。")
    return all_succeeded
