"""Hook 配置和严格协议的低层边界；进程行为由 features/test_hook_process.py 验收。"""

import pytest

from adam.config.hooks import HookCommand, parse_hooks
from adam.config.loader import load_config
from adam.contracts.errors import ConfigError
from adam.contracts.hooks import HookEvent, parse_reply
from tests.test_config import LLM


def test_toml_hook_config_is_explicit_and_loading_never_starts_program(tmp_path):
    main = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    main.write_text("", encoding="utf-8")
    assert not load_config(main).hooks.enabled
    main.write_text(
        "[hooks]\nenabled=true\ntimeout_seconds=2\n"
        '[hooks.before_tool]\nargv=["AdamNeverStartDuringConfig.exe", "argument"]\n',
        encoding="utf-8",
    )
    config = load_config(main)
    assert config.hooks.enabled and config.hooks.timeout_seconds == 2
    assert config.hooks.before_tool.argv == ("AdamNeverStartDuringConfig.exe", "argument")
    assert config.hooks.after_tool is None


@pytest.mark.parametrize(
    "data",
    [
        {"enabled": True},
        {"enabled": 1},
        {"enabled": False, "before_tool": {"argv": []}},
        {"before_tool": {"argv": "python"}},
        {"before_tool": {"argv": ["python"], "env": {"KEY": "not-allowed"}}},
        {"unknown": True},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": True},
        {"max_input_bytes": 0},
        {"max_output_bytes": 1048576},
    ],
)
def test_invalid_config_is_rejected_even_when_disabled(data):
    with pytest.raises(ConfigError):
        parse_hooks(data)


def test_implicit_windows_batch_program_is_not_accepted():
    with pytest.raises(ConfigError):
        HookCommand(("checker.cmd",))


@pytest.mark.parametrize(
    "event,raw",
    [
        (HookEvent.BEFORE_TOOL, b'{"version":1,"decision":"allow","decision":"deny"}'),
        (HookEvent.BEFORE_TOOL, b'{"version":true,"decision":"allow"}'),
        (HookEvent.BEFORE_TOOL, b'{"version":1,"decision":"allow","override":"act"}'),
        (HookEvent.BEFORE_TOOL, b'{"version":1,"decision":"allow","reason":NaN}'),
        (HookEvent.BEFORE_TOOL, b'{"version":1,"decision":"allow","reason":"\\ud800"}'),
        (HookEvent.BEFORE_TOOL, b'{"version":1,"decision":"allow"} {}'),
        (HookEvent.AFTER_TOOL, b'{"version":1,"decision":"deny"}'),
    ],
)
def test_reply_cannot_override_facts_or_use_ambiguous_json(event, raw):
    with pytest.raises(ValueError):
        parse_reply(event, raw)
