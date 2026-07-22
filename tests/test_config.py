"""应用配置模块的单元测试。

这些测试不访问真实模型 API，也不读取开发者本机的 ``.env``。每个测试都显式
隔离配置来源，确保测试结果不会受到个人密钥或机器环境的影响。
"""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from finagent.core.config import Settings


def test_settings_use_safe_development_defaults() -> None:
    """无任何密钥时也应使用适合离线资产面板的安全默认值。"""

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.llm_provider == "bailian"
    assert settings.llm_model == "qwen3.6-flash"
    assert str(settings.llm_base_url) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.llm_enable_thinking is False
    assert settings.llm_temperature == 0.2
    assert settings.llm_max_output_tokens == 1200
    assert settings.llm_api_key is None
    assert settings.goldapi_api_key is None
    assert str(settings.goldapi_base_url) == "https://www.goldapi.io/api"
    assert settings.goldapi_timeout_seconds == 10
    assert settings.goldapi_cache_ttl_seconds == 900
    assert settings.market_data_mode == "fake"
    assert settings.manual_gold_price_max_age_seconds == 900


def test_secret_key_is_masked_when_converted_to_text() -> None:
    """两个 Provider 的 SecretStr 都不应在日志或调试输出中暴露明文。"""

    settings = Settings(
        llm_api_key=SecretStr("super-secret-key"),
        goldapi_api_key=SecretStr("gold-super-secret-key"),
        _env_file=None,  # type: ignore[call-arg]
    )

    assert str(settings.llm_api_key) == "**********"
    assert str(settings.goldapi_api_key) == "**********"
    assert "super-secret-key" not in repr(settings)
    assert "gold-super-secret-key" not in repr(settings)


def test_settings_read_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量应覆盖代码中的默认值，便于不同部署环境独立配置。"""

    monkeypatch.setenv("LLM_API_KEY", "environment-key")
    monkeypatch.setenv("LLM_MODEL", "qwen3.7-plus")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "true")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.6")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "2400")
    monkeypatch.setenv("GOLDAPI_API_KEY", "gold-environment-key")
    monkeypatch.setenv("GOLDAPI_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("GOLDAPI_CACHE_TTL_SECONDS", "1800")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.require_llm_api_key() == "environment-key"
    assert settings.llm_model == "qwen3.7-plus"
    assert settings.llm_enable_thinking is True
    assert settings.llm_temperature == 0.6
    assert settings.llm_max_output_tokens == 2400
    assert settings.require_goldapi_api_key() == "gold-environment-key"
    assert settings.goldapi_timeout_seconds == 15
    assert settings.goldapi_cache_ttl_seconds == 1800


def test_settings_allow_missing_llm_key_but_provider_boundary_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """离线面板允许缺少模型 Key，真正创建模型 Provider 前仍必须失败。"""

    monkeypatch.delenv("LLM_API_KEY", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.llm_api_key is None
    with pytest.raises(ValueError, match="缺少 LLM_API_KEY"):
        settings.require_llm_api_key()


def test_settings_treat_blank_llm_key_as_missing() -> None:
    """复制模板留下的空白模型 Key 应允许面板启动，但聊天仍不能使用。"""

    settings = Settings(
        llm_api_key=SecretStr("   "),
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.llm_api_key is None


def test_settings_allow_missing_optional_goldapi_key() -> None:
    """未使用黄金真实数据时，不应阻断普通CLI和其他离线功能启动。"""

    settings = Settings(
        llm_api_key=SecretStr("test-key"),
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.goldapi_api_key is None
    with pytest.raises(ValueError, match="缺少 GOLDAPI_API_KEY"):
        settings.require_goldapi_api_key()


def test_real_market_mode_requires_goldapi_key() -> None:
    """Real 模式会装配 GoldAPI Provider，缺少 Key 时应在启动组合根前失败。"""

    with pytest.raises(ValidationError, match="MARKET_DATA_MODE=real"):
        Settings(
            market_data_mode="real",
            _env_file=None,  # type: ignore[call-arg]
        )


def test_real_market_mode_accepts_goldapi_key_without_llm_key() -> None:
    """真实资产面板需要 GoldAPI Key，但仍不应该强迫用户配置模型 Key。"""

    settings = Settings(
        market_data_mode="real",
        goldapi_api_key=SecretStr("gold-test-key"),
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.market_data_mode == "real"
    assert settings.llm_api_key is None
    assert settings.require_goldapi_api_key() == "gold-test-key"


def test_settings_treat_blank_goldapi_key_as_missing() -> None:
    """复制.env.example留下的空占位符应视为未启用，而不是阻断其他功能。"""

    settings = Settings(
        llm_api_key=SecretStr("test-key"),
        goldapi_api_key=SecretStr("   "),
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.goldapi_api_key is None
    with pytest.raises(ValueError, match="缺少 GOLDAPI_API_KEY"):
        settings.require_goldapi_api_key()


def test_settings_reject_invalid_temperature() -> None:
    """超出百炼接口范围的温度值必须被拦截，避免构造无效 API 请求。"""

    with pytest.raises(ValidationError, match="llm_temperature"):
        Settings(
            llm_api_key=SecretStr("test-key"),
            llm_temperature=2,
            _env_file=None,  # type: ignore[call-arg]
        )


def test_settings_can_load_a_dotenv_file(tmp_path: Path) -> None:
    """开发者复制出的 .env 文件应能成为本地配置来源。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=dotenv-key\n"
        "LLM_MODEL=qwen3.7-plus\n"
        "LLM_TIMEOUT_SECONDS=45\n"
        "GOLDAPI_API_KEY=gold-dotenv-key\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.require_llm_api_key() == "dotenv-key"
    assert settings.llm_model == "qwen3.7-plus"
    assert settings.llm_timeout_seconds == 45
    assert settings.require_goldapi_api_key() == "gold-dotenv-key"
