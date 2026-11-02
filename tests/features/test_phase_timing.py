"""B34.1：真实评估入口收集阶段；首输出和失败保持实际观测语义。"""

import json

import httpx
import pytest

from adam.contracts.models import Completion, ModelOutput
from scripts.phase_timing import PhaseTiming
from scripts.task_eval import run_suite
from tests.features.test_task_eval_runner import config
from tests.features.test_tool_loop import answer


def test_trial_collects_model_storage_and_unknown_unobserved_phases(tmp_path):
    def transport(task, name, iteration):
        return httpx.MockTransport(
            lambda request: httpx.Response(200, json=answer(json.dumps(task.expected)))
        )

    report = run_suite(
        tmp_path / "run",
        {"base": config(tmp_path)},
        cases=("no-tools",),
        transport_factory=transport,
    )
    run = report["runs"][0]
    assert run["passed"]
    timing = run["phase_timing"]
    assert {r["phase"] for r in timing["records"]} >= {"model", "storage", "first_output"}
    assert {"approval", "tool"} <= set(timing["unobserved"])
    assert all(r["duration_ms"] is None or r["duration_ms"] >= 0 for r in timing["records"])


def test_stream_output_and_failed_nested_operations_keep_evidence(tmp_path):
    timing = PhaseTiming()
    received = []

    class Model:
        def complete(self, messages, **kwargs):
            callback = kwargs["on_output"]
            callback(ModelOutput("text", " "))
            callback(ModelOutput("text", "answer"))
            return Completion({"role": "assistant", "content": "answer"}, (1, 1))

    model = Model()
    timing.bind_model(model)
    model.complete([], model="test", on_output=received.append)
    assert len(received) == 2

    def operation():
        assert timing.wrap("approval", lambda: True)()
        (tmp_path / "actual.txt").write_text("changed")
        raise RuntimeError("failure after write")

    with pytest.raises(RuntimeError):
        timing.wrap("tool", operation)()
    rows = timing.snapshot()["records"]
    assert next(r for r in rows if r["phase"] == "first_output")["duration_ms"] is not None
    tool = next(r for r in rows if r["phase"] == "tool")
    approval = next(r for r in rows if r["phase"] == "approval")
    assert tool["status"] == "failed"
    assert tool["start_ms"] <= approval["start_ms"] <= approval["end_ms"] <= tool["end_ms"]
