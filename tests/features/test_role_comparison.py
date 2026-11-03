"""B41.3：实际工具与Skill的双方案对照，保留总成本和失败分母。"""

import json

import httpx

from adam.config.settings import AgentConfig, LLMConfig
from scripts.role_eval import compare_roles
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_same_tasks_compare_role_chain_and_single_skill_with_actual_file_results(tmp_path):
    stages = []

    def factory(arm, case, stage):
        stages.append((arm, case, stage))
        count = 0

        def respond(request):
            nonlocal count
            count += 1
            body = json.loads(request.content)
            name = "train.py" if case == "repair" else "difference.py"
            if stage == "single":
                assert "careful-edit" in json.dumps(body)
            if count == 1:
                result = tool_response([call("r", "read_file", path=name)])
            elif stage == "investigator":
                result = answer(
                    "Investigated "
                    + name
                    + ": "
                    + (
                        "replace subtraction in add"
                        if case == "repair"
                        else "leave correct difference unchanged"
                    )
                )
            elif case == "repair" and count == 2:
                version = outputs(body)[-1]["version"]
                result = tool_response(
                    [
                        call(
                            "edit",
                            "replace_text",
                            path=name,
                            old_text="a - b",
                            new_text="a + b",
                            expected_version=version,
                        )
                    ]
                )
            else:
                result = answer("done" if case == "repair" else '{"changed":false}')
            return httpx.Response(200, json=result)

        return httpx.MockTransport(respond)

    llm = LLMConfig("https://unused.test", "test", "secret", stream=False)
    report = compare_roles(tmp_path / "normal", llm, transport_factory=factory)
    assert all(row["passed"] for row in report["rows"]), report
    assert report["mode"] == "mock_http"
    assert report["summary"]["single_skill"]["requests"] == 5
    assert report["summary"]["handoff"]["requests"] == 9
    assert report["summary"]["single_skill"]["total_tokens"] == 60
    assert report["summary"]["handoff"]["total_tokens"] == 108
    assert "真实角色收益待验收" in report["conclusion"]
    stages.clear()
    limited = compare_roles(
        tmp_path / "limited",
        llm,
        transport_factory=factory,
        limits=AgentConfig(max_model_requests=2),
    )
    assert all(stage != "executor" for _, _, stage in stages)
    assert all(not row["passed"] for row in limited["rows"] if row["arm"] == "handoff")
    assert limited["summary"]["handoff"]["planned"] == 2
    assert (tmp_path / "limited" / "comparison.json").exists()
