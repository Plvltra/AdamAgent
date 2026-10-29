"""记忆目录跟随主配置解析，独立于会话目录。"""

from pathlib import Path

import pytest

from adam.config.loader import load_config
from adam.contracts.errors import ConfigError
from tests.test_config import LLM


def test_memory_directory_defaults_and_overrides(tmp_path):
    config = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    config.write_text("", encoding="utf-8")
    assert load_config(config).memories_dir == tmp_path / "memories"
    config.write_text('[storage]\nmemories_dir="facts"', encoding="utf-8")
    assert load_config(config).memories_dir == tmp_path / "facts"
    absolute = (tmp_path / "elsewhere").as_posix()
    config.write_text(f'[storage]\nmemories_dir="{absolute}"', encoding="utf-8")
    assert load_config(config).memories_dir == Path(absolute)


@pytest.mark.parametrize("value", ['""', "42", "false", "[]"])
def test_invalid_memory_directory_is_config_error(tmp_path, value):
    config = tmp_path / "config.toml"
    config.write_text(f"[storage]\nmemories_dir={value}", encoding="utf-8")
    with pytest.raises(ConfigError, match="memories_dir"):
        load_config(config)
