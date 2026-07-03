"""应用配置模块的单元测试。

这些测试不访问真实模型 API，也不读取开发者本机的 ``.env``。每个测试都显式
隔离配置来源，确保测试结果不会受到个人密钥或机器环境的影响。
"""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from finagent.core.config import Settings


def test_settings_use_safe_development_defaults() -> None:
    """提供密钥后应使用适合第一阶段开发的默认模型参数。"""

    settings = Settings(
        llm_api_key=SecretStr("test-key"),
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.llm_provider == "bailian"
    assert settings.llm_model == "qwen3.6-flash"
    assert str(settings.llm_base_url) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.llm_enable_thinking is False
    assert settings.llm_temperature == 0.2
    assert settings.llm_max_output_tokens == 1200


def test_secret_key_is_masked_when_converted_to_text() -> None:
    """SecretStr 不应在日志或调试输出中暴露真实密钥。"""

    settings = Settings(
        llm_api_key=SecretStr("super-secret-key"),
        _env_file=None,  # type: ignore[call-arg]
    )

    assert str(settings.llm_api_key) == "**********"
    assert "super-secret-key" not in repr(settings)


def test_settings_read_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量应覆盖代码中的默认值，便于不同部署环境独立配置。"""

    monkeypatch.setenv("LLM_API_KEY", "environment-key")
    monkeypatch.setenv("LLM_MODEL", "qwen3.7-plus")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "true")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.6")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "2400")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.llm_api_key.get_secret_value() == "environment-key"
    assert settings.llm_model == "qwen3.7-plus"
    assert settings.llm_enable_thinking is True
    assert settings.llm_temperature == 0.6
    assert settings.llm_max_output_tokens == 2400


def test_settings_reject_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少 API Key 时应立即报错，而不是等到真正请求模型时才失败。"""

    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with pytest.raises(ValidationError, match="llm_api_key"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_reject_blank_api_key() -> None:
    """只有空格的密钥没有意义，应被自定义校验器拒绝。"""

    with pytest.raises(ValidationError, match="LLM_API_KEY 不能为空"):
        Settings(
            llm_api_key=SecretStr("   "),
            _env_file=None,  # type: ignore[call-arg]
        )


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
        "LLM_API_KEY=dotenv-key\nLLM_MODEL=qwen3.7-plus\nLLM_TIMEOUT_SECONDS=45\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.llm_api_key.get_secret_value() == "dotenv-key"
    assert settings.llm_model == "qwen3.7-plus"
    assert settings.llm_timeout_seconds == 45
