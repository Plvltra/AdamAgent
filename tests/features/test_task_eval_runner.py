"""B31.2：真实应用+模拟HTTP运行矩阵，保留失败、未知用量及独立环境。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import AppConfig, LLMConfig, MemoryConfig, ReflectionConfig
from scripts.task_eval import run_suite
from tests.features.test_tool_loop import answer


def config(tmp_path):
    return AppConfig(
        LLMConfig("https://unused.test", "test", "do-not-persist-key", stream=False),
        tmp_path / "unused",
        "unused",
        reflection=ReflectionConfig(enabled=False),
    )


def test_matrix_counts_wrong_answers_and_preserves_independent_trials(tmp_path):
    base = config(tmp_path)
    configs = {"on": base, "off": replace(base, memory=MemoryConfig(enabled=False))}
    roots = []

    def transport(task, name, iteration):
        roots.append(task.workspace)

        def handle(request):
            text = json.dumps(task.expected)
            if name == "off" and iteration == 0 and task.case.case_id == "counterexample":
                text = '{"changed":true}'
            return httpx.Response(200, json=answer(text))

        return httpx.MockTransport(handle)

    report = run_suite(
        tmp_path / "results",
        configs,
        repeats=2,
        cases=("counterexample", "no-tools"),
        transport_factory=transport,
        prices_cny_per_million=(1.0, 2.0),
    )
    assert report["mode"] == "mock_http"
    assert report["planned"] == report["attempted"] == len(set(roots)) == 8
    assert report["passed"] == 7 and report["success_rate"] == 7 / 8
    assert report["known_tokens"] == 8 * 12
    assert not report["unknown_usage"]
    assert all(r["estimated_total_cost_cny"] == 14 / 1_000_000 for r in report["runs"])
    assert len({r["session_id"] for r in report["runs"]}) == 8
    manifest = (tmp_path / "results" / "manifest.json").read_text()
    assert "do-not-persist-key" not in manifest
    assert json.loads(manifest)["configs"]["off"]["memory"]["enabled"] is False


def test_unknown_usage_stops_remaining_trials_and_live_requires_opt_in(tmp_path):
    configs = {"base": config(tmp_path)}
    with pytest.raises(ValueError, match="explicit"):
        run_suite(tmp_path / "disabled", configs)
    assert not (tmp_path / "disabled").exists()

    def transport(task, name, iteration):
        return httpx.MockTransport(lambda request: httpx.Response(500, text="remote failed"))

    report = run_suite(
        tmp_path / "failed",
        configs,
        cases=("no-tools", "counterexample"),
        transport_factory=transport,
    )
    assert report["planned"] == 2 and report["attempted"] == 1
    assert report["passed"] == 0 and report["unknown_usage"]
    assert [r["status"] for r in report["runs"]] == ["failed", "not_run"]
    assert report["runs"][0]["estimated_total_cost_cny"] is None
