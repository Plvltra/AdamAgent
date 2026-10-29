"""E5.3：比较协作的工程结果与开销；模拟决策不能证明真实模型质量提升。"""

import json
import subprocess
import sys
from time import perf_counter

import httpx
import pytest

from tests.features.test_independent_review import reviewer
from tests.features.test_subagents import application, call, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("defect", [True, False])
def test_executor_repairs_real_defect_and_review_can_also_add_no_benefit(tmp_path, defect):
    target = tmp_path / "calc.py"
    original = (
        "def add(a, b):\n    return a - b\n" if defect else "def add(a, b):\n    return a + b\n"
    )
    target.write_text(original, encoding="utf-8")
    calls = 0
    approved = []
    metrics = {}

    def child(request):
        body = json.loads(request.content)
        results = outputs(body)
        if not results:
            return reviewer(request)
        path = json.loads(
            next(m["content"] for m in body["messages"] if m["role"] == "user").split("\n", 1)[0]
        )["targets"][0]["path"]
        broken = any("a - b" in line["text"] for line in results[-1]["lines"])
        report = {
            "verdict": "findings" if broken else "no_findings",
            "summary": "加法实现为减法" if broken else "符合加法条件",
            "evidence": ["read"],
            "findings": [
                {
                    "path": path,
                    "line": 2,
                    "severity": "high",
                    "message": "将减法修正为加法",
                    "evidence_id": "read",
                }
            ]
            if broken
            else [],
        }
        return httpx.Response(200, json=answer(json.dumps(report)))

    def parent(request):
        nonlocal calls
        calls += 1
        results = outputs(json.loads(request.content))
        if calls == 1 or (defect and calls == 5):
            batch = [
                call(
                    f"ask{calls}",
                    "request_review",
                    goal="加法",
                    criteria="add(2,3)==5",
                    paths=["calc.py"],
                )
            ]
        elif calls == 2 or (defect and calls == 6):
            batch = [call(f"collect{calls}", "collect_review", review_id=results[-1]["review_id"])]
        elif calls == 3 and defect:
            assert results[-1]["result"]["verdict"] == "findings"
            batch = [call("read-main", "read_file", path="calc.py")]
        elif calls == 4 and defect:
            batch = [
                call(
                    "fix",
                    "replace_text",
                    path="calc.py",
                    old_text="a - b",
                    new_text="a + b",
                    expected_version=results[-1]["version"],
                )
            ]
        else:
            assert results[-1]["result"]["verdict"] == "no_findings"
            return httpx.Response(200, json=answer("已完成独立检查"))
        return httpx.Response(200, json=tool_response(batch))

    started = perf_counter()
    with application(
        tmp_path, parent, child, confirm_edit=lambda plan: approved.append(plan) or True
    ) as (app, store):
        app.send("检查加法并修正")
        reviews = store.load_reviews(app.session.session_id)
        assert [r["result"]["verdict"] for r in reviews] == (
            ["findings", "no_findings"] if defect else ["no_findings"]
        )
        assert len(approved) == int(defect)
        metrics.update(review_requests=calls + 2 * len(reviews), recorded_tokens=app.usage().tokens)
        assert metrics["recorded_tokens"] == metrics["review_requests"] * 12
    metrics["elapsed_seconds"] = perf_counter() - started
    verified = subprocess.run(
        [sys.executable, "-c", "from calc import add; assert add(2,3)==5"],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert verified.returncode == 0, verified.stderr
    if not defect:
        assert target.read_text(encoding="utf-8") == original
        # 同一个已正确任务，单 Agent 直接回答只需一次请求；审查增加成本且未改变结果。
        metrics.update(single_requests=1, single_tokens=12, extra_requests=4, extra_tokens=48)
        assert metrics["review_requests"] == 5
    (tmp_path / "comparison.json").write_text(json.dumps(metrics), encoding="utf-8")
