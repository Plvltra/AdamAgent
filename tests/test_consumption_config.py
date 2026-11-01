"""显式单位配置只接受独立上限和十进制字符串单价；默认不影响旧运行。"""

import pytest

from adam.config.consumption import ConsumptionConfig, load_consumption_config, parse_consumption
from adam.config.loader import load_config
from adam.contracts.errors import ConfigError
from tests.test_config import LLM


def test_default_and_explicit_zero_caps_roundtrip():
    assert not ConsumptionConfig().enabled
    config = parse_consumption(
        {
            "enabled": True,
            "max_images": 0,
            "max_audio_ms": 0,
            "max_calls": 2,
            "prices": {"exact": {"currency": "USD", "per_unit": {"calls": "0.001"}}},
        }
    )
    assert config.limits().images == 0 and config.limits().audio_ms == 0
    assert config.price("other") is None
    assert parse_consumption(config.as_record()) == config


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"unknown": 1},
        {"enabled": 1},
        {"max_calls": 1},
        {"enabled": True, "max_images": -1},
        {"enabled": True, "max_audio_ms": True},
        {"enabled": True, "max_calls": 10**19},
        {"enabled": True, "prices": []},
        {"enabled": True, "prices": {"a": None}},
        {"enabled": True, "prices": {"a": {"currency": "usd", "per_unit": {"calls": "1"}}}},
        {"enabled": True, "prices": {"a": {"currency": "USD", "per_unit": {"calls": 0.1}}}},
        {"enabled": True, "prices": {"a": {"currency": "USD", "per_unit": {"calls": "NaN"}}}},
        {"enabled": True, "prices": {"a": {"currency": "USD", "per_unit": {"seconds": "1"}}}},
    ],
)
def test_invalid_config_is_not_silently_disabled_or_free(value):
    with pytest.raises(ConfigError):
        parse_consumption(value)


def test_toml_main_and_keyless_independent_entry_agree(tmp_path):
    text = (
        "[consumption]\nenabled=true\nmax_calls=2\nmax_images=1\n"
        '[consumption.prices."test"]\ncurrency="USD"\n'
        '[consumption.prices."test".per_unit]\nimages="0.3"\ncalls="0.01"\n'
    )
    path = tmp_path / "consumption.toml"
    path.write_text(text, encoding="utf-8")
    independent = load_consumption_config(path)
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    assert load_config(path).consumption == independent
    path.write_text(text + '[llm]\napi_key="not-read"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_consumption_config(path)
