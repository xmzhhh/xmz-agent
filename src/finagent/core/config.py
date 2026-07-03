"""应用配置模型。

本模块将环境变量集中转换为经过类型校验的 Python 对象。业务代码只依赖
``Settings``，不直接散落调用 ``os.getenv``。这样可以统一默认值、错误信息、
安全策略和测试方式，也方便以后增加本地模型或其他云端模型 Provider。
"""

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Literal 会同时约束运行时配置和静态类型，避免把拼写错误悄悄传到 API 请求中。
ProviderName = Literal["bailian"]


class Settings(BaseSettings):
    """FinAgent 第一阶段所需的应用配置。

    配置优先从操作系统环境变量读取；开发环境还会读取项目根目录的 ``.env``。
    ``.env`` 只用于本机开发，部署时应由运行平台安全地注入环境变量。

    Attributes:
        llm_provider: 模型服务提供方。第一阶段只实现阿里云百炼。
        llm_model: 发起请求时使用的模型 ID。
        llm_api_key: API 密钥，使用 SecretStr 避免在日志和 repr 中显示明文。
        llm_base_url: API 基础地址，为后续替换 Provider 保留配置边界。
        llm_enable_thinking: 是否启用千问的思考模式。
        llm_temperature: 控制生成随机性的采样温度。
        llm_max_output_tokens: 单次响应允许生成的最大 token 数。
        llm_timeout_seconds: 单次 HTTP 请求的超时时间。
        llm_max_retries: 遇到暂时性错误时允许的最大重试次数。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        validate_default=True,
    )

    llm_provider: ProviderName = "bailian"
    llm_model: str = Field(default="qwen3.6-flash", min_length=1)
    llm_api_key: SecretStr
    llm_base_url: AnyHttpUrl = AnyHttpUrl("https://dashscope.aliyuncs.com/compatible-mode/v1")
    llm_enable_thinking: bool = False
    llm_temperature: float = Field(default=0.2, ge=0, lt=2)
    llm_max_output_tokens: int = Field(default=1200, ge=1, le=128_000)
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=10)

    @field_validator("llm_api_key")
    @classmethod
    def api_key_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        """拒绝空白密钥，让配置问题在启动阶段暴露。

        这里只检查是否为空，不校验固定前缀。不同账户和兼容服务可能使用不同
        格式，过度限制格式反而会降低配置层的可复用性。

        Args:
            value: Pydantic 已转换完成的密钥对象。

        Returns:
            通过非空校验的原始 SecretStr。

        Raises:
            ValueError: 密钥为空或只包含空白字符时抛出。
        """

        if not value.get_secret_value().strip():
            raise ValueError("LLM_API_KEY 不能为空，请在本地 .env 中配置真实密钥")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """加载并缓存应用配置。

    配置对象是只读的，并且一个进程内只需解析一次环境变量。缓存还能确保不同
    模块拿到同一份配置快照，避免运行过程中环境变量变化导致行为不一致。

    Returns:
        当前进程共享的 Settings 实例。
    """

    return Settings()  # type: ignore[call-arg]
