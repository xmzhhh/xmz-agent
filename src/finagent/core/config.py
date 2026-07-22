"""应用配置模型。

本模块将环境变量集中转换为经过类型校验的 Python 对象。业务代码只依赖
``Settings``，不直接散落调用 ``os.getenv``。这样可以统一默认值、错误信息、
安全策略和测试方式，也方便以后增加本地模型或其他云端模型 Provider。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Literal 会同时约束运行时配置和静态类型，避免把拼写错误悄悄传到 API 请求中。
ProviderName = Literal["bailian"]
MarketDataMode = Literal["fake", "real"]

# 不能直接使用相对路径 ".env"：相对路径会以进程的当前工作目录为基准，而 PyCharm
# 运行 scripts 下的文件时，工作目录可能不是项目根目录。根据当前源码文件反向定位
# 项目根目录后，无论从 PyCharm、CMD、pytest 还是安装后的命令入口启动，开发环境的
# 配置文件位置都保持一致。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """FinAgent 各阶段共享的应用配置。

    配置优先从操作系统环境变量读取；开发环境还会读取项目根目录的 ``.env``。
    ``.env`` 只用于本机开发，部署时应由运行平台安全地注入环境变量。

    Attributes:
        llm_provider: 模型服务提供方。第一阶段只实现阿里云百炼。
        llm_model: 发起请求时使用的模型 ID。
        llm_api_key: 可选 API 密钥；只有创建模型 Provider 时才强制要求。
        llm_base_url: API 基础地址，为后续替换 Provider 保留配置边界。
        llm_enable_thinking: 是否启用千问的思考模式。
        llm_temperature: 控制生成随机性的采样温度。
        llm_max_output_tokens: 单次响应允许生成的最大 token 数。
        llm_timeout_seconds: 单次 HTTP 请求的超时时间。
        llm_max_retries: 遇到暂时性错误时允许的最大重试次数。
        goldapi_api_key: GoldAPI 密钥；未启用真实黄金 Provider 时允许不配置。
        goldapi_base_url: GoldAPI REST API 的基础地址。
        goldapi_timeout_seconds: GoldAPI HTTP 客户端的请求超时。
        goldapi_cache_ttl_seconds: 国际黄金参考价的进程内缓存秒数。
        market_data_mode: 资产面板使用固定 Fake 行情还是真实行情 Provider。
        manual_gold_price_max_age_seconds: 手工京东卖出价允许使用的最大秒数。
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        validate_default=True,
    )

    llm_provider: ProviderName = "bailian"
    llm_model: str = Field(default="qwen3.6-flash", min_length=1)
    llm_api_key: SecretStr | None = None
    llm_base_url: AnyHttpUrl = AnyHttpUrl("https://dashscope.aliyuncs.com/compatible-mode/v1")
    llm_enable_thinking: bool = False
    llm_temperature: float = Field(default=0.2, ge=0, lt=2)
    llm_max_output_tokens: int = Field(default=1200, ge=1, le=128_000)
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    goldapi_api_key: SecretStr | None = None
    goldapi_base_url: AnyHttpUrl = AnyHttpUrl("https://www.goldapi.io/api")
    goldapi_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    goldapi_cache_ttl_seconds: float = Field(default=900.0, gt=0, le=86_400)
    market_data_mode: MarketDataMode = "fake"
    manual_gold_price_max_age_seconds: int = Field(default=900, gt=0, le=86_400)

    @field_validator("llm_api_key")
    @classmethod
    def normalize_blank_llm_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        """把空模型密钥转换为“尚未配置”。

        离线资产面板不调用大模型，因此不能因为缺少模型密钥而启动失败。真正创建
        ``BailianModelProvider`` 时会调用 :meth:`require_llm_api_key` 严格校验。

        Args:
            value: Pydantic 转换后的可选密钥。

        Returns:
            未配置或只有空白时返回 ``None``；否则返回原始 ``SecretStr``。
        """

        if value is not None and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("goldapi_api_key")
    @classmethod
    def normalize_blank_goldapi_api_key(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        """把模板中的空 GoldAPI 密钥转换为“尚未配置”。

        GoldAPI 是可选数据源，因此普通 CLI 聊天和离线测试不应被迫配置它。真正创建
        GoldAPI Provider 时会调用 :meth:`require_goldapi_api_key`，在那里检查缺失情况。
        这样用户复制包含 ``GOLDAPI_API_KEY=`` 占位符的 ``.env.example`` 后，其他功能
        仍然可以启动。

        Args:
            value: Pydantic 从环境变量转换出的可选密钥。

        Returns:
            未配置或内容为空时返回 ``None``；否则返回原始 ``SecretStr``。
        """

        if value is not None and not value.get_secret_value().strip():
            return None
        return value

    def require_goldapi_api_key(self) -> str:
        """为真实 GoldAPI Provider 返回必需的明文密钥。

        配置模型平时使用 ``SecretStr`` 防止调试输出泄密；只有构造认证请求头的 Provider
        边界需要短暂取得明文。调用方不得记录、拼接到异常或向用户回显该返回值。

        Returns:
            去除首尾空白后的 GoldAPI API Key。

        Raises:
            ValueError: 当前环境没有配置 ``GOLDAPI_API_KEY``。
        """

        if self.goldapi_api_key is None:
            raise ValueError("缺少 GOLDAPI_API_KEY，请在项目根目录的本地 .env 中配置")
        return self.goldapi_api_key.get_secret_value().strip()

    def require_llm_api_key(self) -> str:
        """为模型 Provider 返回必需的明文密钥。

        离线 Dashboard 只读取普通配置，不调用本方法；聊天命令和模型 Provider 必须调用，
        因而仍会在发起网络请求前暴露缺失配置。

        Raises:
            ValueError: 当前环境没有配置 ``LLM_API_KEY``。
        """

        if self.llm_api_key is None:
            raise ValueError("缺少 LLM_API_KEY，请在项目根目录的本地 .env 中配置")
        return self.llm_api_key.get_secret_value().strip()

    @model_validator(mode="after")
    def real_market_data_requires_goldapi_key(self) -> "Settings":
        """Real 模式必须在应用启动前确认 GoldAPI Key 已配置。"""

        if self.market_data_mode == "real" and self.goldapi_api_key is None:
            raise ValueError("MARKET_DATA_MODE=real 时必须配置 GOLDAPI_API_KEY")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """加载并缓存应用配置。

    配置对象是只读的，并且一个进程内只需解析一次环境变量。缓存还能确保不同
    模块拿到同一份配置快照，避免运行过程中环境变量变化导致行为不一致。

    Returns:
        当前进程共享的 Settings 实例。
    """

    return Settings()
