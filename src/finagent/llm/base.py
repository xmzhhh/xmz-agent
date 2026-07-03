"""模型 Provider 的抽象协议。

这里不实现任何厂商调用，只规定“一个模型服务适配器必须具备什么能力”。
使用 Protocol 而不是要求所有实现继承某个基类，可以降低耦合：只要对象提供
相同签名的异步方法，静态类型检查器就会认为它满足 ModelProvider 协议。
"""

from typing import Protocol, runtime_checkable

from finagent.llm.schemas import ModelRequest, ModelResponse


@runtime_checkable
class ModelProvider(Protocol):
    """所有大模型服务适配器必须遵守的异步协议。

    后续的 BailianModelProvider、OllamaModelProvider 等实现都要把厂商特有的请求
    和响应转换为统一的 ModelRequest 与 ModelResponse。Agent 因而无需知道实际
    使用的是哪家模型服务。
    """

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """向模型发送一次请求并返回标准化结果。

        Args:
            request: 与具体厂商无关的统一模型请求。

        Returns:
            统一的模型响应，其中可能包含文本、工具调用或二者兼有。

        Raises:
            Exception: Provider 应把厂商异常转换成项目自定义异常；异常体系将在
                后续实现真实 Provider 时补充。
        """

        ...

    async def close(self) -> None:
        """释放 Provider 持有的网络连接等资源。

        即使某个 SDK 暂时不需要手动关闭，也保留统一生命周期接口，方便未来接入
        复用 HTTP Client 的实现。
        """

        ...
