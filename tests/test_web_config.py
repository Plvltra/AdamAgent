"""网页可选配置的解析与直接构造边界。"""

import pytest

from adam.config.loader import load_config
from adam.config.settings import WebConfig
from adam.contracts.errors import ConfigError


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"max_bytes": 0},
        {"max_chars": True},
        {"max_redirects": -1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
    ],
)
def test_web_config_rejects_ambiguous_or_unbounded_direct_values(changes):
    with pytest.raises(ConfigError):
        WebConfig(**changes)


@pytest.mark.parametrize(
    "section,expected",
    [
        ("", False),
        (
            "[web]\nenabled=true\nmax_bytes=500\nmax_chars=123\nmax_redirects=0\ntimeout_seconds=1.5",
            True,
        ),
    ],
)
def test_optional_web_config_parses_without_network(tmp_path, section, expected):
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://unused.test"\nmodel="test"\napi_key="not-a-real-key"',
        encoding="utf-8",
    )
    path = tmp_path / "config.toml"
    path.write_text(section, encoding="utf-8")
    config = load_config(path)
    assert config.web.enabled is expected
    if expected:
        assert config.web.max_bytes == 500 and config.web.max_chars == 123
        assert config.web.max_redirects == 0 and config.web.timeout_seconds == 1.5


@pytest.mark.parametrize(
    "section",
    [
        "[web]\nallow_private=true",
        "[web]\nmax_bytes=true",
        "[web]\ntimeout_seconds=0",
    ],
)
def test_web_toml_cannot_introduce_network_bypasses_or_invalid_limits(tmp_path, section):
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://unused.test"\nmodel="test"\napi_key="not-a-real-key"',
        encoding="utf-8",
    )
    path = tmp_path / "config.toml"
    path.write_text(section, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
