"""启动业务：无效配置在创建 HTTP 客户端和会话数据之前终止。"""

import sys

import pytest

import adam.cli as cli


@pytest.mark.parametrize("invalid_section", ["main", "llm"])
def test_invalid_startup_configuration_stops_before_network(
    tmp_path, monkeypatch, capsys, invalid_section
):
    """主配置或模型配置无效时，真实 main 返回错误且不创建网络客户端或会话。"""
    config = tmp_path / "config.toml"
    config.write_text(
        "[chat]\nsystem_prompt=" + ("42" if invalid_section == "main" else '"test"') + "\n",
        encoding="utf-8",
    )
    model = "42" if invalid_section == "llm" else '"test"'
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://example.test"\nmodel=' + model + '\napi_key="test-secret"\n',
        encoding="utf-8",
    )

    def forbidden_client(*args, **kwargs):
        pytest.fail("Invalid configuration must fail before HTTP client creation")

    monkeypatch.setattr(cli.httpx, "Client", forbidden_client)
    monkeypatch.setattr(
        sys, "argv", ["adam", "--config", str(config), "--workspace", str(tmp_path)]
    )
    assert cli.main() == 2
    output = capsys.readouterr().out
    assert "配置错误" in output and "test-secret" not in output
    assert not (tmp_path / "sessions").exists()
