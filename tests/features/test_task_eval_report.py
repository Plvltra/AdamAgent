"""B31.3：实际生产记忆开关、正常反例及缺失结果配对。"""

import json

import httpx

from scripts.task_eval import run_suite
from scripts.task_eval_report import ablation_configs, compare, markdown
from tests.features.test_task_eval_runner import config
from tests.features.test_tool_loop import answer


def test_ablation_observes_real_memory_selection_and_reports_controls(tmp_path):
    configurations = ablation_configs(config(tmp_path), "memory")

    def transport(task, name, iteration):
        def handle(request):
            body = json.loads(request.content)
            if task.case.case_id == "memory-basic":
                observed = "开发同事王伟喜欢饮茶" in json.dumps(body, ensure_ascii=False)
                assert observed is (name == "enabled")
                reply = {"drink": "tea" if observed else "coffee"}
            else:
                reply = {"needs_tools": False}
            return httpx.Response(200, json=answer(json.dumps(reply)))

        return httpx.MockTransport(handle)

    root = tmp_path / "suite"
    result = run_suite(
        root, configurations, cases=("memory-basic", "no-tools"), transport_factory=transport
    )
    assert result["passed"] == 3
    selections = [r["memory_selections"][0] for r in result["runs"] if r["case"] == "memory-basic"]
    assert selections[0]["status"] == "disabled" and selections[0]["records"] == []
    assert selections[1]["records"]
    report = compare(root)
    pair = report["comparisons"][0]
    assert pair["single_mechanism_toggle"] and pair["provider_identity_recorded"]
    assert pair["pairs"]["improved"] == ["memory-basic#0"]
    assert pair["pairs"]["both_passed"] == ["no-tools#0"]
    assert report["configs"]["enabled"]["control_passed"] == 1
    assert "不能推断模型质量" in markdown(report)
    (root / "enabled-memory-basic-0.json").unlink()
    missing = compare(root)
    assert missing["configs"]["enabled"]["planned"] == 2
    assert missing["configs"]["enabled"]["passed"] == 1
    assert missing["configs"]["enabled"]["unknown_usage"]
    assert missing["comparisons"][0]["pairs"]["unavailable"] == ["memory-basic#0"]
