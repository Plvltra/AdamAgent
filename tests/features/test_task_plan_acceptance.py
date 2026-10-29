"""E3.4：同一两文件任务比较普通工具循环与显式计划；远端模拟，本地动作真实。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

from adam.config.settings import DocumentConfig, MemoryConfig, PlanningConfig
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import application, call, outputs
from tests.features.test_task_planning import plan_app, step
from tests.features.test_tool_loop import answer, tool_response


def task_steps(phase):
    """固定三个业务步骤；只有对应动作结束后才引用其工具 ID。"""
    definitions = (
        ("inspect", "read both files", ["read_a", "read_b"]),
        ("edit", "update both files", ["edit_a", "edit_b"]),
        ("verify", "check both results", ["check"]),
    )
    return [
        {
            "step_id": key,
            "content": content,
            "status": "completed"
            if index < phase
            else "in_progress"
            if index == phase
            else "pending",
            "evidence": evidence if index < phase else [],
            "reason": "",
        }
        for index, (key, content, evidence) in enumerate(definitions)
    ]


def run_two_file_task(workspace, data, planned):
    """相同审批和本地动作，仅增加计划调用；返回落盘请求、工具数及输入估算。"""
    for name in ("a", "b"):
        (workspace / f"{name}.py").write_bytes(b"VALUE = 1\n")
    config = replace(
        config_at(data),
        planning=PlanningConfig(enabled=planned),
        memory=MemoryConfig(enabled=False),
    )
    requests, approvals, plans = [], [], []

    def confirm(action):
        approvals.append(action)
        return True

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        results = {
            message["tool_call_id"]: json.loads(message["content"])
            for message in body["messages"]
            if message["role"] == "tool"
        }
        assert ("update_plan" in {tool["function"]["name"] for tool in body["tools"]}) == planned
        if index == 1:
            calls = [call(f"read_{name}", "read_file", path=f"{name}.py") for name in ("a", "b")]
        elif index == 2:
            calls = [
                call(
                    f"edit_{name}",
                    "replace_text",
                    path=f"{name}.py",
                    old_text="VALUE = 1",
                    new_text="VALUE = 2",
                    expected_version=results[f"read_{name}"]["version"],
                )
                for name in ("a", "b")
            ]
        elif index == 3:
            assert all(results[f"edit_{name}"]["status"] == "applied" for name in ("a", "b"))
            calls = [
                call(
                    "check",
                    "run_command",
                    argv=[
                        sys.executable,
                        "-c",
                        "from pathlib import Path; "
                        "assert all(Path(n).read_bytes() == b'VALUE = 2\\n' for n in ('a.py','b.py')); "
                        "print('both verified')",
                    ],
                )
            ]
        elif index == 4 and planned:
            assert results["check"]["exit_code"] == 0
            calls = []
        else:
            assert results["check"]["exit_code"] == 0
            return httpx.Response(200, json=answer("both files updated and verified"))
        if planned:
            calls.insert(0, call(f"plan{index}", "update_plan", steps=task_steps(index - 1)))
        return httpx.Response(200, json=tool_response(calls))

    with plan_app(workspace, handler, config=config, confirm=confirm, plans=plans) as (app, store):
        reply = app.send("Update VALUE from 1 to 2 in a.py and b.py, then verify both files.")
        assert reply == "both files updated and verified"
        assert len(approvals) == 3
        assert all((workspace / f"{name}.py").read_bytes() == b"VALUE = 2\n" for name in ("a", "b"))
        session_id = app.session.session_id
        events = store.load_events(session_id)
        started = [e["payload"] for e in events if e["type"] == "request_started"]
        tools = [e["payload"] for e in events if e["type"] == "tool_finished"]
        assert len(started) == len(requests)
        assert all(json.loads(tool["output"])["ok"] is True for tool in tools)
        assert [edit["status"] for edit in store.load_edits(session_id)] == ["applied", "applied"]
        execution = store.load_executions(session_id)[0]
        assert execution["exit_code"] == 0 and "both verified" in execution["stdout"]
        if planned:
            report = app.plan_report()
            assert report.turn_status == "succeeded" and not report.snapshot.unfinished
            assert [s.to_dict() for s in report.snapshot.steps] == task_steps(3)
            assert [p.snapshot.version for p in plans] == [1, 2, 3, 4]
            assert report.snapshot.replan_count == 0
            # 步骤证据追溯实际工具，计划和工作工具均属于该轮真实请求。
            assert all(tool["turn_id"] == report.snapshot.turn_id for tool in tools)
            assert {p.snapshot.request_id for p in plans} <= {r["request_id"] for r in started}
        else:
            assert app.plan_report() is None and not plans
        metrics = {
            "requests": len(started),
            "tool_calls": len(tools),
            "estimated_input_tokens": sum(r["context"]["estimated_input_tokens"] for r in started),
        }

    with plan_app(workspace, handler, config=config) as (app, store):
        app.resume(session_id)
        assert store.load_messages(session_id)[-1].content == reply
        assert (app.plan_report() is not None) == planned
        assert len(requests) == metrics["requests"]
    return metrics


def test_same_two_file_task_with_and_without_explicit_plan(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    direct = run_two_file_task(workspace, tmp_path / "direct", False)
    planned = run_two_file_task(workspace, tmp_path / "planned", True)
    assert (direct["requests"], direct["tool_calls"]) == (4, 5)
    assert (planned["requests"], planned["tool_calls"]) == (5, 9)
    assert planned["estimated_input_tokens"] > direct["estimated_input_tokens"]
    # 只报告本地估算；模拟 HTTP 的 usage 不能作为真实模型成本。
    print(json.dumps({"direct": direct, "planned": planned}))


@pytest.mark.parametrize("child_succeeds", [True, False])
def test_child_results_can_support_plan_but_child_cannot_update_it(tmp_path, child_succeeds):
    parent_requests, child_requests = [], []

    def child(request):
        body = json.loads(request.content)
        child_requests.append(body)
        assert "update_plan" not in {tool["function"]["name"] for tool in body["tools"]}
        if len(child_requests) == 1:
            # 模型即使伪造未注册的工具调用，也不能更新主计划。
            return httpx.Response(
                200, json=tool_response([call("forged", "update_plan", steps=[step()])])
            )
        assert outputs(body)[-1]["ok"] is False
        if child_succeeds:
            return httpx.Response(200, json=answer("child analysis"))
        # 已知用量的协议失败，避免把共享未知消费拦截混入证据校验场景。
        return httpx.Response(
            200, json={"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        )

    def parent(request):
        body = json.loads(request.content)
        parent_requests.append(body)
        index = len(parent_requests)
        if index == 1:
            calls = [
                call("plan1", "update_plan", steps=[step("in_progress")]),
                call("spawn", "spawn_agent", name="helper", task="analyze task"),
            ]
        elif index == 2:
            calls = [call("wait", "wait_agents", agent_ids=[])]
        elif index == 3:
            agents = outputs(body)[-1]["agents"]
            assert agents and all(
                agent["status"] == ("completed" if child_succeeds else "failed") for agent in agents
            )
            calls = [call("plan2", "update_plan", steps=[step("completed", ["wait"])])]
        elif index == 4 and not child_succeeds:
            assert outputs(body)[-1]["ok"] is False
            calls = [call("plan3", "update_plan", steps=[step("failed", ["wait"], "child failed")])]
        else:
            return httpx.Response(200, json=answer("collected"))
        return httpx.Response(200, json=tool_response(calls))

    with application(tmp_path, parent, child) as (app, store):
        app.send("delegate analysis with a plan")
        report = app.plan_report()
        assert report.snapshot.version == 2
        assert report.snapshot.steps[0].status == ("completed" if child_succeeds else "failed")
        assert report.snapshot.steps[0].evidence == ("wait",)
        assert len(child_requests) == 2


def test_document_retrieval_can_supply_real_plan_evidence(tmp_path):
    """同一应用注册文档检索和计划，完成依据指向实际检索结果并可重启读取。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "guide.md"
    source.write_text("项目代码必须使用绝对导入。", encoding="utf-8")
    config = replace(config_at(tmp_path), documents=DocumentConfig(roots=(docs,)))
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        tools = {tool["function"]["name"] for tool in body["tools"]}
        assert {"search_documents", "update_plan"} <= tools
        if len(requests) == 1:
            calls = [
                call("plan1", "update_plan", steps=[step("in_progress")]),
                call("search", "search_documents", query="项目绝对导入"),
            ]
        elif len(requests) == 2:
            result = outputs(body)[-1]
            assert result["ok"] and result["status"] == "matches"
            evidence = result["results"][0]
            assert evidence["path"] == str(source)
            assert evidence["start_line"] == 1 and "绝对导入" in evidence["text"]
            calls = [call("plan2", "update_plan", steps=[step("completed", ["search"])])]
        else:
            assert outputs(body)[-1]["ok"]
            return httpx.Response(200, json=answer("依据 guide.md:1，项目使用绝对导入。"))
        return httpx.Response(200, json=tool_response(calls))

    with plan_app(tmp_path, handler, config=config) as (app, store):
        assert "guide.md:1" in app.send("先列计划，查项目导入约定并说明来源")
        session_id = app.session.session_id
        report = app.plan_report()
        assert report.turn_status == "succeeded"
        assert report.snapshot.steps[0].status == "completed"
        assert report.snapshot.steps[0].evidence == ("search",)
        search_result = next(
            event["payload"]
            for event in store.load_events(session_id)
            if event["type"] == "tool_finished" and event["payload"]["tool_call_id"] == "search"
        )
        assert search_result["turn_id"] == report.snapshot.turn_id
        assert json.loads(search_result["output"])["results"][0]["path"] == str(source)

    with plan_app(tmp_path, handler, config=config) as (app, store):
        app.resume(session_id)
        assert app.plan_report().snapshot.steps[0].evidence == ("search",)
        assert len(requests) == 3


def test_simple_question_does_not_create_a_plan(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=answer("simple answer"))

    with plan_app(tmp_path, handler) as (app, store):
        assert app.send("hello") == "simple answer"
        assert app.plan_report() is None and len(requests) == 1
        assert not any(
            e["type"] == "plan_updated" for e in store.load_events(app.session.session_id)
        )
