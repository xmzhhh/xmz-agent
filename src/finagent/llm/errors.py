"""模型访问层的统一异常。

Provider 会把不同 SDK 的异常转换为本模块中的稳定类型。上层 Agent 因而只需判断
“鉴权失败、限流、超时或其他服务错误”，不必了解每家模型厂商的异常类。
"""


class ModelProviderError(RuntimeError):
    """所有模型 Provider 异常的基类。"""


class ModelAuthenticationError(ModelProviderError):
    """API 密钥无效、过期或无权访问指定模型。"""


class ModelRateLimitError(ModelProviderError):
    """请求频率或账户配额受到限制。"""


class ModelTimeoutError(ModelProviderError):
    """模型请求在配置的超时时间内未完成。"""


class ModelConnectionError(ModelProviderError):
    """本机因网络、代理、DNS 或 TLS 问题无法连接模型服务。"""


class ModelResponseError(ModelProviderError):
    """厂商响应缺少必要字段或无法转换成项目统一模型。"""
