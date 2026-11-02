"""B32.3：固定响应不冒充新模型结果，未知副作用拒绝续跑。"""

import pytest

from adam.contracts.models import Completion
from scripts.prefix_regression import DecisionCapture, choose_next, replay_fixed


def test_fixed_response_and_new_decision_have_distinct_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class Model:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            return Completion({"role": "assistant", "content": str(self.calls)}, (1, 1))

    model = Model()
    capture = DecisionCapture(model, workspace, tmp_path / "capture")
    capture.complete([{"role": "user", "content": "task"}], model="test")
    reference, response = capture.snapshots[0], capture.responses[0]
    allowed = ({"name": "final_answer", "arguments": {"text": "2"}},)
    fixed = replay_fixed(reference, response, allowed)
    assert fixed["mode"] == "fixed_response"
    assert not fixed["verification"]["passed"] and model.calls == 1
    new = choose_next(reference, model, allowed)
    assert new["mode"] == "new_decision" and new["verification"]["passed"]
    assert model.calls == 2
    capture.complete([{"role": "user", "content": "different"}], model="test")
    with pytest.raises(ValueError, match="another prefix"):
        replay_fixed(capture.snapshots[1], response, allowed)
    response.path.write_bytes(response.path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        replay_fixed(reference, response, allowed)


def test_external_effect_prefix_refuses_both_modes_before_request(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class Model:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            return Completion({"role": "assistant", "content": "done"}, None)

    model = Model()
    capture = DecisionCapture(model, workspace, tmp_path / "capture")
    capture.complete(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "external",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": '{"argv":["publish"]}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "external", "content": "ok"},
        ],
        model="test",
    )
    reference = capture.snapshots[0]
    with pytest.raises(ValueError, match="external state"):
        choose_next(reference, model, ({"name": "final_answer"},))
    with pytest.raises(ValueError, match="external state"):
        replay_fixed(reference, capture.responses[0], ({"name": "final_answer"},))
    assert model.calls == 1
