"""B32.2：请求前文件快照、冻结前缀重选和动作集合限制。"""

import json

import pytest

from adam.contracts.models import Completion
from scripts.prefix_regression import DecisionCapture, choose_next, load, restore, verify_actions


def action(name, **arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "next",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_frozen_decision_restores_prior_files_and_accepts_alternative_action(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("before", encoding="utf-8")
    messages = [
        {"role": "system", "content": "constraints"},
        {"role": "user", "content": "find fact"},
    ]

    class Original:
        def complete(self, actual, **kwargs):
            assert actual == messages
            return Completion(action("read_file", path="missing.txt"), (1, 1))

    capture = DecisionCapture(Original(), workspace, tmp_path / "snapshots")
    original = capture.complete(messages, model="test", tools=[], max_tokens=32)
    reference = capture.snapshots[0]
    (workspace / "fact.txt").write_text("after", encoding="utf-8")
    restored = tmp_path / "restored"
    restore(reference, restored)
    assert (restored / "fact.txt").read_text() == "before"
    assert (workspace / "fact.txt").read_text() == "after"
    allowed = ({"name": "read_file", "arguments": {"path": "fact.txt"}}, {"name": "search_text"})
    forbidden = ({"name": "run_command"},)
    assert not verify_actions(original.message, allowed, forbidden)["passed"]

    class Retry:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            assert messages == load(reference)["request"]["messages"]
            assert kwargs["max_tokens"] == 32
            return Completion(action("search_text", query="fact"), (2, 1))

    client = Retry()
    result = choose_next(reference, client, allowed, forbidden)
    assert result["mode"] == "new_decision" and result["verification"]["passed"]
    assert client.calls == 1
    assert not verify_actions(
        action("run_command", argv=["bad"]), ({"name": "run_command"},), forbidden
    )["passed"]
    assert (restored / "fact.txt").read_text() == "before"  # 验收动作不会执行工具。


def test_changed_prefix_is_rejected_before_new_request(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class Model:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            return Completion({"role": "assistant", "content": "done"}, (1, 1))

    model = Model()
    capture = DecisionCapture(model, workspace, tmp_path / "snapshots")
    capture.complete([{"role": "user", "content": "task"}], model="test")
    reference = capture.snapshots[0]
    reference.path.write_bytes(reference.path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        choose_next(reference, model, ({"name": "final_answer"},))
    assert model.calls == 1
