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
    assert config.db_path == directory / "data" / "adam.db"
    main.write_text(
        '[storage]\ndb_path="history/custom.db"\n[chat]\nsystem_prompt=""', encoding="utf-8-sig"
    )
    alternate = tmp_path / "alternate.toml"
    alternate.write_text(LLM.replace('model="test"', 'model="other"'), encoding="utf-8")
    config = load_config(main, Path("alternate.toml"))
    assert config.db_path == directory / "history" / "custom.db"
    assert config.system_prompt == ""
    assert config.llm.model == "other"
    absolute = (tmp_path / "absolute.db").as_posix()
    main.write_text(f'[storage]\ndb_path="{absolute}"', encoding="utf-8")
    assert load_config(main).db_path == Path(absolute)


@pytest.mark.parametrize(
    "content",
    [
        '[storage]\ndb_path=""',
        "[storage]\ndb_path=1",
        "[chat]\nsystem_prompt=42",
        '[storage]\ndb_paths="x"',
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


def test_cli_overrides_database_and_llm(tmp_path, monkeypatch):
    import sys

    import adam.cli as cli

    main = tmp_path / "config.toml"
    main.write_text('[storage]\ndb_path="unused.db"', encoding="utf-8")
    (tmp_path / "alternate.toml").write_text(LLM, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["adam", "--config", str(main), "--llm-config", "alternate.toml", "--db", "override.db"],
    )
    calls = []
    monkeypatch.setattr(
        cli, "chat", lambda client, prompt, store, **kwargs: calls.append(client.config.model)
    )
    assert cli.main() == 0
    assert calls == ["test"]
    assert (tmp_path / "override.db").is_file()
    assert not (tmp_path / "unused.db").exists()
