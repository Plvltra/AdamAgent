"""计划配置在直接构造和 TOML 入口使用相同范围校验。"""

import pytest

from adam.config.loader import load_config
from adam.config.settings import PlanningConfig
from adam.contracts.errors import ConfigError


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": 1},
        {"max_steps": True},
        {"max_steps": 0},
        {"max_steps": 65},
        {"max_replans": -1},
        {"max_replans": True},
    ],
)
def test_invalid_planning_limits_are_rejected(values):
    with pytest.raises(ConfigError):
        PlanningConfig(**values)


def test_planning_toml_overrides_and_unknown_fields(tmp_path):
    path = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://example.test"\nmodel="test"\napi_key="unused"\n',
        encoding="utf-8",
    )
    path.write_text("[planning]\nenabled=false\nmax_steps=4\nmax_replans=0\n", encoding="utf-8")
    config = load_config(path)
    assert config.planning == PlanningConfig(enabled=False, max_steps=4, max_replans=0)
    path.write_text("[planning]\nunknown=1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
