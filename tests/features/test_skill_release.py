"""B37.2：模拟HTTP但实际加载Skill/编辑文件，双集评估后人工采用与回滚。"""

import json
from dataclasses import replace

import httpx

from adam.config.settings import ToolDiscoveryConfig
from scripts.experience_evidence import create
from scripts.failure_tasks import generate
from scripts.skill_proposal import propose
from scripts.skill_release import decide, evaluate, rollback
from tests.features.test_subagents import call
from tests.features.test_task_eval_runner import config
from tests.features.test_tool_loop import answer, tool_response


def test_dual_suite_real_skill_and_edit_then_reject_accept_rollback(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = tmp_path / "lesson"
    create(
        ledger,
        lesson="读取确认后修复运算并保留其他文件",
        applies_when="代码运算修复",
        exceptions="正确实现不改",
        environment="v1",
    )
    proposal = tmp_path / "proposal"
    propose(ledger, workspace, proposal, name="check-code", environment="v1")
    generate(tmp_path / "dataset")
    loaded = []

    def transport(task, name, iteration):
        count = 0

        def handle(request):
            nonlocal count
            count += 1
            body = json.loads(request.content)
            if name == "candidate" and count == 1:
                return httpx.Response(
                    200, json=tool_response([call("skill", "load_skill", skill_id="check-code")])
                )
            if name == "candidate" and count == 2:
                loaded.append(body["messages"][-1]["content"])
            if task.case.category == "operator_repair" and name == "candidate":
                filename = next(p.name for p in task.workspace.glob("*.py"))
                if count == 2:
                    return httpx.Response(
                        200, json=tool_response([call("read", "read_file", path=filename)])
                    )
                if count == 3:
                    read = json.loads(body["messages"][-1]["content"])
                    return httpx.Response(
                        200,
                        json=tool_response(
                            [
                                call(
                                    "edit",
                                    "replace_text",
                                    path=filename,
                                    old_text="a - b",
                                    new_text="a + b",
                                    expected_version=read["version"],
                                )
                            ]
                        ),
                    )
            return httpx.Response(200, json=answer(json.dumps(task.expected)))

        return httpx.MockTransport(handle)

    output = tmp_path / "evaluation"
    gate = evaluate(
        proposal,
        tmp_path / "dataset/dataset.json",
        output,
        replace(config(tmp_path), tool_discovery=ToolDiscoveryConfig(mode="full")),
        transport_factory=transport,
    )
    assert gate["eligible"] and gate["mode"] == "mock_http"
    assert len(loaded) == 6 and all("读取确认" in item for item in loaded)
    target = workspace / "skills/check-code/SKILL.md"
    assert not decide(proposal, output / "gate.json", lambda review: False)
    assert not target.exists()
    assert decide(proposal, output / "gate.json", lambda review: review["evaluation"]["eligible"])
    assert target.read_bytes() == (proposal / "candidate/check-code/SKILL.md").read_bytes()
    assert rollback(proposal, lambda review: True) and not target.exists()
    actions = [
        json.loads(p.read_text())["action"] for p in sorted((proposal / "history").glob("*.json"))
    ]
    assert actions == ["rejected", "approved", "installed", "rolled_back"]
