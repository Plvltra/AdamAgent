"""B41.1：可信角色配置收窄模型与实际工具，不用提示词替代执行约束。"""

import json
from pathlib import Path

import pytest

from adam.config.roles import load_roles
from adam.contracts.errors import ConfigError
from adam.contracts.execution import ExecutionMode
from adam.tool_calls.factory import create_tool_registry


def test_role_models_and_request_caps_are_host_bounded(tmp_path):
    example = Path(__file__).resolve().parents[2] / "roles.example.toml"
    roles = load_roles(example, frozenset({"deepseek-chat"}), 8)
    assert set(roles) == {"investigator", "executor"}
    assert roles["investigator"].max_model_requests == 4
    with pytest.raises(ConfigError):
        load_roles(example, frozenset({"other-model"}), 8)
    with pytest.raises(ConfigError):
        load_roles(example, frozenset({"deepseek-chat"}), 2)
    bad = tmp_path / "roles.toml"
    bad.write_text(
        example.read_text(encoding="utf-8").replace(
            "max_model_requests = 4", "max_model_requests = true"
        )
    )
    with pytest.raises(ConfigError):
        load_roles(bad, frozenset({"deepseek-chat"}), 8)


def test_restricted_registry_preserves_real_permissions_mode_and_approval(tmp_path):
    example = Path(__file__).resolve().parents[2] / "roles.example.toml"
    roles = load_roles(example, frozenset({"deepseek-chat"}), 8)
    path = tmp_path / "a.txt"
    path.write_text("original")
    approvals, guards = [], []

    def deny(plan):
        approvals.append(plan.path)
        return False

    full = create_tool_registry(tmp_path, confirm_edit=deny).guarded(guards.append)
    investigation = full.restricted(roles["investigator"].tools, roles["investigator"].capabilities)
    read = json.loads(investigation.execute("read_file", json.dumps({"path": "a.txt"})))
    assert read["ok"] and guards
    arguments = json.dumps(
        {
            "path": "a.txt",
            "old_text": "original",
            "new_text": "changed",
            "expected_version": read["version"],
        }
    )
    assert not json.loads(investigation.execute("replace_text", arguments))["ok"]
    assert not approvals
    with pytest.raises(ValueError):
        full.restricted(frozenset({"replace_text"}), roles["investigator"].capabilities)
    executor = full.restricted(roles["executor"].tools, roles["executor"].capabilities)
    assert json.loads(executor.execute("replace_text", arguments))["status"] == "denied"
    assert approvals == [path]
    assert not json.loads(
        executor.with_mode(lambda: ExecutionMode.PLAN).execute("replace_text", arguments)
    )["ok"]
    assert approvals == [path]
    assert path.read_text() == "original"
