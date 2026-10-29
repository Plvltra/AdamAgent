"""E5.2：真实子线程读取与审查，HTTP 只模拟模型的选择和结论。"""

import json

import httpx
import pytest

from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


def reviewer(request):
    """独立读取请求中的目标，结论引用真实工具 ID，不采用主 Agent 的自评。"""
    body = json.loads(request.content)
    task = next(m["content"] for m in body["messages"] if m["role"] == "user")
    target = json.loads(task.split("\n", 1)[0])["targets"][0]["path"]
    if not outputs(body):
        return httpx.Response(200, json=tool_response([call("read", "read_file", path=target)]))
    result = {"verdict": "no_findings", "summary": "符合条件", "evidence": ["read"], "findings": []}
    return httpx.Response(200, json=answer(json.dumps(result)))


@pytest.mark.parametrize("mode", ["valid", "invented", "changed", "deleted"])
def test_review_collects_actual_evidence_and_rejects_false_or_stale_verdict(tmp_path, mode):
    target = tmp_path / "calc.py"
    target.write_text("value = 5\n", encoding="utf-8")
    count, responses, notices = 0, [], []

    def child(request):
        if mode == "invented":
            return httpx.Response(
                200,
                json=answer(
                    json.dumps(
                        {
                            "verdict": "no_findings",
                            "summary": "没问题",
                            "evidence": [],
                            "findings": [],
                        }
                    )
                ),
            )
        response = reviewer(request)
        if mode == "deleted" and outputs(json.loads(request.content)):
            # 删除发生在子 Agent 真正读取之后、主 Agent 收集之前。
            target.unlink()
        return response

    def parent(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call(
                            "ask",
                            "request_review",
                            goal="值为5",
                            criteria="value = 5",
                            paths=["calc.py"],
                        ),
                    ]
                ),
            )
        if count == 2:
            rid = outputs(body)[-1]["review_id"]
            if mode == "changed":
                # 模拟另一个终端修改文件；不通过 Adam 的审批入口。
                target.write_text("value = 6\n", encoding="utf-8")
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call("blocked", "run_command", argv=["git", "--version"]),
                        call("collect", "collect_review", review_id=rid),
                    ]
                ),
            )
        responses.extend(outputs(body))
        return httpx.Response(200, json=answer("审查结果已汇总"))

    with application(tmp_path, parent, child, notices=notices) as (app, store):
        assert app.send("独立检查该文件") == "审查结果已汇总"
        records = store.load_reviews(app.session.session_id)
        assert (
            records[0]["status"]
            == {
                "valid": "completed",
                "invented": "invalid",
                "changed": "stale",
                "deleted": "stale",
            }[mode]
        )
        assert not responses[-2]["ok"]
        assert store.load_executions(app.session.session_id) == []
        if mode == "deleted":
            assert records[0]["reads"][0]["complete"]
            assert records[0]["result"] is None
        if mode == "valid":
            assert records[0]["reads"][0]["complete"]
            assert records[0]["result"]["verdict"] == "no_findings"
            assert app.usage().tokens == 5 * 12
        else:
            assert any("未完成" in notice or "文件已变化" in notice for notice in notices)


def test_review_round_limit_is_not_bypassed_by_generic_send(tmp_path):
    from dataclasses import replace

    from adam.config.settings import ReviewConfig
    from tests.features.test_memory_context import config_at

    (tmp_path / "calc.py").write_text("value = 5\n", encoding="utf-8")
    count = 0
    captured = []
    config = replace(config_at(tmp_path), review=ReviewConfig(max_rounds=1))

    def parent(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count == 1:
            calls = [call("ask", "request_review", goal="值", criteria="5", paths=["calc.py"])]
        elif count == 2:
            calls = [call("collect", "collect_review", review_id=outputs(body)[-1]["review_id"])]
        elif count == 3:
            agent_id = json.loads(index_for(app).read_text(encoding="utf-8"))["agents"][0][
                "agent_id"
            ]
            calls = [
                call("bypass", "send_message", agent_id=agent_id, task="再审"),
                call("over", "request_review", goal="再审", criteria="5", paths=["calc.py"]),
            ]
        else:
            captured.extend(outputs(body))
            return httpx.Response(200, json=answer("达到上限，不继续"))
        return httpx.Response(200, json=tool_response(calls))

    with application(tmp_path, parent, reviewer, config=config) as (app, store):
        app.send("检查")
        assert "request_review" in captured[-2]["error"]
        assert "上限" in captured[-1]["error"]
        assert len(store.load_reviews(app.session.session_id)) == 1
