"""B42.3：真实工具来源驱动预算策略，对照简单/独立任务与子任务失败回收。"""

import json

import httpx

from adam.config.settings import LLMConfig
from scripts.delegation_eval import compare_delegation
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response


def test_budgeted_comparison_preserves_results_costs_and_failed_children(tmp_path):
    fail_child = False

    def factory(arm, case, stage):
        count = 0

        def respond(request):
            nonlocal count
            count += 1
            if stage == "child":
                if fail_child:
                    result = answer("A")
                    result.pop("usage")  # 模拟远端返回内容却无法提供结算信息。
                elif count == 1:
                    result = tool_response([call("r", "read_file", path="a.txt")])
                else:
                    result = answer("A")
            elif arm == "self":
                if count == 1:
                    calls = [call("a", "read_file", path="a.txt")]
                    if case == "independent":
                        calls.append(call("b", "read_file", path="b.txt"))
                    result = tool_response(calls)
                else:
                    result = answer(
                        json.dumps({"a": "A"} if case == "simple" else {"a": "A", "b": "B"})
                    )
            elif count == 1:
                result = tool_response(
                    [
                        call(
                            "spawn",
                            "spawn_agent",
                            name="reader",
                            task="read a.txt and report its content",
                        )
                    ]
                )
            elif count == 2:
                result = tool_response(
                    [call("a", "read_file", path="a.txt")]
                    if case == "simple"
                    else [
                        call("b", "read_file", path="b.txt"),
                        call("wait", "wait_agents", agent_ids=[]),
                    ]
                )
            else:
                result = answer(
                    json.dumps({"a": "A"} if case == "simple" else {"a": "A", "b": "B"})
                )
            return httpx.Response(200, json=result)

        return httpx.MockTransport(respond)

    llm = LLMConfig("https://unused.test", "test", "secret", stream=False)
    report = compare_delegation(tmp_path / "normal", llm, factory=factory)
    assert all(row["passed"] and row["all_children_terminal"] for row in report["rows"]), report
    rows = {(row["case"], row["arm"]): row for row in report["rows"]}
    simple = rows["simple", "budgeted"]
    assert simple["decisions"][0]["initial"]["action"] == "self" and not simple["children"]
    independent = rows["independent", "budgeted"]
    assert independent["decisions"][0]["initial"]["action"] == "delegate"
    assert len(independent["children"]) == 1
    assert independent["requests"] == 5 and independent["total_tokens"] == 60
    assert rows["independent", "self"]["requests"] == 2
    assert report["mode"] == "mock_http"
    fail_child = True
    failed = compare_delegation(tmp_path / "failed", llm, factory=factory)
    failed_row = next(
        row for row in failed["rows"] if row["case"] == "independent" and row["arm"] == "budgeted"
    )
    assert not failed_row["passed"] and failed_row["unknown_usage"]
    assert failed_row["total_tokens"] is None
    assert failed_row["all_children_terminal"]
    assert failed_row["children"][0]["status"] == "failed"
    assert len(failed["rows"]) == 4
