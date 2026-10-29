"""反思配置在 TOML 与直接构造时均有明确上限。"""

import pytest

from adam.config.loader import load_config
from adam.config.settings import ReflectionConfig
from adam.contracts.errors import ConfigError


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": 1},
        {"max_per_turn": True},
        {"max_per_turn": 0},
        {"max_per_turn": 5},
        {"max_input_tokens": 0},
        {"max_output_tokens": 4097},
    ],
)
def test_invalid_reflection_configuration(values):
    with pytest.raises(ConfigError):
        ReflectionConfig(**values)


def test_reflection_toml_and_unknown_field(tmp_path):
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://unused.test"\nmodel="test"\napi_key="unused"\n', encoding="utf-8"
    )
    path = tmp_path / "config.toml"
    path.write_text("[reflection]\nenabled=false\nmax_per_turn=2\n", encoding="utf-8")
    assert load_config(path).reflection == ReflectionConfig(enabled=False, max_per_turn=2)
    path.write_text("[reflection]\nunknown=1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
