from pathlib import Path

import pytest

from adam.config import ConfigError, load_config

LLM = '[llm]\nbase_url="https://example.test"\nmodel="test"\napi_key="secret"\n'


def test_paths_defaults_and_llm_override(tmp_path, monkeypatch):
    directory = tmp_path / "settings"
    directory.mkdir()
    main = directory / "config.toml"
    main.write_text("", encoding="utf-8")
    (directory / "llm.toml").write_text(LLM, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = load_config(main)
    assert config.sessions_dir == directory / "sessions"
    main.write_text(
        '[storage]\nsessions_dir="history/custom"\n[chat]\nsystem_prompt=""', encoding="utf-8-sig"
    )
    alternate = tmp_path / "alternate.toml"
    alternate.write_text(LLM.replace('model="test"', 'model="other"'), encoding="utf-8")
    config = load_config(main, Path("alternate.toml"))
    assert config.sessions_dir == directory / "history" / "custom"
    assert config.system_prompt == ""
    assert config.llm.model == "other"
    absolute = (tmp_path / "absolute").as_posix()
    main.write_text(f'[storage]\nsessions_dir="{absolute}"', encoding="utf-8")
    assert load_config(main).sessions_dir == Path(absolute)


@pytest.mark.parametrize(
    "content",
    [
        '[storage]\nsessions_dir=""',
        "[storage]\nsessions_dir=1",
        "[chat]\nsystem_prompt=42",
        '[storage]\nsessions_dirs="x"',
        "[chat]\nunknown=true",
        "storage=1",
        "chat=1",
        "[llm]",
        '[chat]\nsystem_prompt="secret',
    ],
)
def test_invalid_app_config(tmp_path, content):
    main = tmp_path / "config.toml"
    main.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(main)
    assert "secret" not in str(error.value)


def test_missing_config_files(tmp_path):
    main = tmp_path / "config.toml"
    with pytest.raises(ConfigError, match="主配置"):
        load_config(main)
    main.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="模型配置"):
        load_config(main)


def test_home_and_cli_llm_override(tmp_path, monkeypatch):
    import sys

    import adam.cli as cli
    from adam.config import adam_home

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ADAM_HOME", str(home))
    assert adam_home() == home
    (home / "config.toml").write_text('[storage]\nsessions_dir="history"', encoding="utf-8")
    (tmp_path / "alternate.toml").write_text(LLM, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["adam", "--llm-config", "alternate.toml"])
    calls = []
    monkeypatch.setattr(
        cli, "chat", lambda client, prompt, store, **kwargs: calls.append(client.config.model)
    )
    assert cli.main() == 0
    assert calls == ["test"]
    assert (home / "history").is_dir()
    monkeypatch.delenv("ADAM_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert adam_home() == tmp_path / ".adam"


def test_old_database_setting_is_rejected(tmp_path):
    main = tmp_path / "config.toml"
    main.write_text('[storage]\ndb_path="old.db"', encoding="utf-8")
    with pytest.raises(ConfigError, match="已移除"):
        load_config(main)


@pytest.mark.parametrize(
    "extra",
    [
        "[agent]\nmax_model_requests=0",
        "[agent]\nmax_tool_calls=true",
        "[permissions]\nadditional_read_dirs=1",
        "[tool_limits]\nmax_result_chars=1",
        "[agent]\nunknown=1",
    ],
)
def test_invalid_tool_and_agent_settings(tmp_path, extra):
    main = tmp_path / "config.toml"
    main.write_text(extra, encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(main)


def test_tool_configuration_relative_roots(tmp_path):
    main = tmp_path / "config.toml"
    main.write_text(
        '[permissions]\nadditional_read_dirs=["extra"]\n[agent]\nmax_session_tokens=100',
        encoding="utf-8",
    )
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    config = load_config(main)
    assert config.permissions.additional_read_dirs == (tmp_path / "extra",)
    assert config.agent.max_session_tokens == 100


def test_example_config_is_loadable(tmp_path):
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    main = tmp_path / "config.toml"
    main.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    config = load_config(main)
    assert config.tool_limits.max_read_lines > 0
    assert config.permissions.additional_read_dirs == ()


def test_unknown_field_is_reported_without_value(tmp_path):
    main = tmp_path / "config.toml"
    main.write_text('[tool_limits]\nmax_result_char="private-value"', encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(main)
    assert "max_result_char" in str(error.value)
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        "context=1",
        "[context]\nauto_compact=1",
        "[context]\ncompression_target_ratio=0.9",
        "[context]\ncompression_target_ratio=nan",
        "[context]\ncompression_target_ratio=true",
        "[context]\nprune_tool_results=1",
        "[context]\ncompression_trigger_ratio=nan",
        "[context]\ncompression_trigger_ratio=true",
        "[context]\ntool_prune_tail_chars=-1",
        "[context]\ntool_prune_threshold_chars=2000",
        "[context]\nretain_recent_turns=-1",
        "[context]\nretain_recent_turns=true",
        "[context]\nsummary_max_output_tokens=0",
        "[context]\nsummary_max_output_tokens=true",
        "[context]\nmax_input_tokens=true",
        "[context]\nmax_input_tokens=0",
        "[context]\nsafety_margin_tokens=-1",
        "[context]\nsafety_margin_tokens=true",
        "[context]\nmax_input_tokens=100\nsafety_margin_tokens=100",
        '[context]\nunknown="private-value"',
    ],
)
def test_invalid_context_budget_configuration(tmp_path, content):
    main = tmp_path / "config.toml"
    main.write_text(content, encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(main)
    assert "context" in str(caught.value) and "private-value" not in str(caught.value)


def test_context_defaults_and_custom_budget(tmp_path):
    main = tmp_path / "config.toml"
    main.write_text("", encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    defaults = load_config(main)
    assert defaults.context.max_input_tokens == 32768
    assert defaults.context.safety_margin_tokens == 2048
    main.write_text("[context]\nmax_input_tokens=100\nsafety_margin_tokens=0", encoding="utf-8")
    custom = load_config(main)
    assert custom.context.max_input_tokens == 100 and custom.context.safety_margin_tokens == 0
    assert custom.agent == defaults.agent
