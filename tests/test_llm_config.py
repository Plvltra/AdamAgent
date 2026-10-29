"""模型连接配置的字段校验与凭据错误处理。"""

import pytest

from adam.config.loader import load_llm_config
from adam.contracts.errors import ConfigError

BASE = '[llm]\nbase_url = "https://example.test/v1/"\nmodel = "test"\n'


def test_config_modes(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(BASE + 'api_key_env = "ADAM_TEST_KEY"\n', encoding="utf-8-sig")
    monkeypatch.setenv("ADAM_TEST_KEY", "environment-secret")
    config = load_llm_config(path)
    assert config.api_key == "environment-secret"
    assert config.base_url == "https://example.test/v1"
    assert "environment-secret" not in repr(config)
    path.write_text(BASE + 'api_key = "literal-secret"\n', encoding="utf-8")
    assert load_llm_config(path).api_key == "literal-secret"


@pytest.mark.parametrize(
    "extra",
    [
        "",
        'api_key = ""',
        'api_key = "secret"\napi_key_env = "TEST"',
        'api_key_env = "ADAM_MISSING_KEY"',
        'api_key = "secret"\ntimeout_seconds = -1',
        'api_key = "secret"\ntimeout_seconds = true',
        'api_key = "secret"\ntimeout_seconds = nan',
        'api_key = "secret"\nsystem_prompt = 42',
        'api_key = "secret"\ntimeout = 30',
    ],
)
def test_config_errors(tmp_path, monkeypatch, extra):
    monkeypatch.delenv("ADAM_MISSING_KEY", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(BASE + extra, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_llm_config(path)


def test_malformed_toml_does_not_expose_secret(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BASE + 'api_key = "secret', encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_llm_config(path)
    assert "secret" not in str(error.value)
