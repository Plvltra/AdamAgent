"""T4.2：用户切换模式，真实工具、会话日志和子任务都遵循同一个受控模式。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

from adam.cli.main import CommandResult, chat, handle_command
from adam.contracts.errors import AgentError, BudgetError
from adam.contracts.execution import ExecutionMode
from adam.session.storage import SessionStore
from tests.features.test_explicit_skills import no_request, skill_application
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_task_planning import plan_app, step
from tests.features.test_tool_loop import answer, tool_response


def test_cli_plan_then_act_shares_mode_with_requests_tools_and_approval(tmp_path):
    target = tmp_path / "code.txt"
    target.write_bytes(b"before")
    marker = tmp_path / "ran"
    payloads, shown, approvals = [], [], []
    inputs = iter(["/mode plan", "调查文件", "/mode act", "执行修改", "/exit"])
    version = ""

    def changes(suffix):
        return [
            call(
                "edit-" + suffix,
                "replace_text",
                path="code.txt",
                old_text="before",
                new_text="after",
                expected_version=version,
            ),
            call(
                "command-" + suffix,
                "run_command",
                argv=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                ],
            ),
        ]

    def respond(request):
        nonlocal version
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        payload = json.loads(request.content)
        payloads.append(payload)
        count = len(payloads)
        mode = "plan" if count <= 3 else "act"
        assert f"当前执行模式：{mode}。" in payload["messages"][0]["content"]
        if count == 1:
            batch = [
                call("plan", "update_plan", steps=[step("in_progress")]),
                call("read", "read_file", path="code.txt"),
            ]
        elif count == 2:
            assert all(item["ok"] for item in outputs(payload))
            version = outputs(payload)[-1]["version"]
            batch = [call("finish-plan", "update_plan", steps=[step("completed", ["read"])])]
            batch.extend(changes("blocked"))
        elif count == 3:
            assert all(item["status"] == "mode_blocked" for item in outputs(payload)[-2:])
            assert approvals == [] and target.read_bytes() == b"before" and not marker.exists()
            return httpx.Response(200, json=answer("方案已准备，执行需要用户切换"))
        elif count == 4:
            batch = changes("approved")
        else:
            assert outputs(payload)[-2]["status"] == "applied"
            assert outputs(payload)[-1]["exit_code"] == 0
            return httpx.Response(200, json=answer("修改和命令均已批准执行"))
        return httpx.Response(200, json=tool_response(batch))

    with plan_app(tmp_path, respond, confirm=lambda item: approvals.append(item) or True) as (
        app,
        store,
    ):
        chat(app, read=lambda _: next(inputs), write=shown.append)
        sid = app.session.session_id
        events = store.load_events(sid)
        requests = [e["payload"] for e in events if e["type"] == "request_started"]
        assert [r["context"]["execution_mode"] for r in requests] == ["plan"] * 3 + ["act"] * 2
        plan_turn = requests[0]["turn_id"]
        assert store.feedback_sources(sid, plan_turn)["failures"] == []
        assert store.load_plan(sid).snapshot.steps[0].status == "completed"
        assert len(store.load_edits(sid)) == len(store.load_executions(sid)) == 1
        assert [message.content for message in app.history() if message.role == "user"] == [
            "调查文件",
            "执行修改",
        ]
    assert len(approvals) == 2 and target.read_bytes() == b"after" and marker.read_text() == "ran"
    assert [line.split(" — ")[0] for line in shown if line.startswith("模式>")] == [
        "模式> act",
        "模式> plan",
        "模式> act",
    ]


def test_mode_commands_are_local_and_session_switches_do_not_restore_old_mode(tmp_path):
    config = config_at(tmp_path)
    shown = []
    with skill_application(tmp_path, config, no_request) as (app, store):
        assert app.mode is ExecutionMode.ACT
        for command in ["/mode", "/mode plan", "/mode"]:
            assert handle_command(command, app, shown.append) is CommandResult.HANDLED
        assert app.mode is ExecutionMode.PLAN
        assert store.list_sessions() == []
        first = app.new("A").session_id
        second = app.new("B").session_id
        app.resume(first)
        assert app.mode is ExecutionMode.PLAN
        handle_command("/mode act", app, shown.append)
        app.resume(second)
        assert app.mode is ExecutionMode.ACT
        assert all(store.load_messages(s.session_id) == [] for s in store.list_sessions())
        handle_command("/mode plan", app, shown.append)
    with skill_application(tmp_path, config, no_request) as (app, _):
        app.resume(first)
        assert app.mode is ExecutionMode.ACT
        assert app.preview_context().execution_mode is ExecutionMode.ACT


@pytest.mark.parametrize("value", ["PLAN", "unknown", "plan act", "act extra"])
def test_invalid_mode_keeps_current_setting_and_does_not_create_session(tmp_path, value):
    with skill_application(tmp_path, config_at(tmp_path), no_request) as (app, store):
        app.select_mode("plan")
        with pytest.raises(AgentError, match="模式未改变"):
            handle_command("/mode " + value, app, lambda _: None)
        assert app.mode is ExecutionMode.PLAN and store.list_sessions() == []


def test_mode_cannot_change_while_turn_is_running_but_is_released_afterward(tmp_path):
    def respond(request):
        payload = json.loads(request.content)
        assert "当前执行模式：plan。" in payload["messages"][0]["content"]
        # 在请求边界模拟另一个界面回调；不能让已发出的 plan 请求按 act 执行。
        with pytest.raises(AgentError, match="尚未结束"):
            app.select_mode("act")
        with pytest.raises(AgentError, match="运行中的任务"):
            app.send("重入任务")
        assert app.mode is ExecutionMode.PLAN
        return httpx.Response(200, json=answer("仍在规划"))

    with skill_application(tmp_path, config_at(tmp_path), respond) as (app, store):
        app.select_mode("plan")
        assert app.send("调查") == "仍在规划"
        assert len(store.load_turns(app.session.session_id)) == 1
        app.select_mode("act")
        assert app.mode is ExecutionMode.ACT


@pytest.mark.parametrize("failure", ["api", "cancel", "skill"])
def test_failed_or_cancelled_turn_releases_mode_without_replaying(tmp_path, failure):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == "cancel":
            raise KeyboardInterrupt()
        return httpx.Response(500, json={"error": "simulated provider failure"})

    with skill_application(tmp_path, config_at(tmp_path), respond) as (app, _):
        app.select_mode("plan")
        with pytest.raises(KeyboardInterrupt if failure == "cancel" else AgentError):
            app.send("调查", skill_id="missing" if failure == "skill" else None)
        app.select_mode("act")
        assert app.mode is ExecutionMode.ACT
        assert len(requests) == (0 if failure == "skill" else 1)


def test_mode_instruction_is_protected_by_input_budget(tmp_path):
    config = config_at(tmp_path)
    with skill_application(tmp_path, config, no_request) as (app, _):
        act_estimate = app.preview_context("检查").estimate.input_tokens
        app.select_mode("plan")
        plan_estimate = app.preview_context("检查").estimate.input_tokens
        assert plan_estimate > act_estimate
    config = replace(
        config,
        context=replace(config.context, max_input_tokens=plan_estimate - 1, safety_margin_tokens=0),
    )
    with skill_application(tmp_path, config, no_request) as (app, store):
        app.select_mode("plan")
        with pytest.raises(BudgetError):
            app.send("检查")
        events = store.load_events(app.session.session_id)
        assert not any(e["type"] == "request_started" for e in events)
        app.select_mode("act")
        assert app.preview_context("检查").budgeted_input_tokens <= plan_estimate - 1


def test_child_run_inherits_mode_and_followup_captures_new_parent_mode(tmp_path):
    (tmp_path / "evidence.txt").write_text("evidence", encoding="utf-8")
    marker = tmp_path / "child-must-not-run"
    parent_count, child_count = 0, 0
    child_id = ""
    expected_mode = "plan"

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        payload = json.loads(request.content)
        assert f"当前执行模式：{expected_mode}。" in payload["messages"][0]["content"]
        stage = (parent_count - 1) % 3
        if stage == 0:
            operation = (
                call("spawn", "spawn_agent", name="helper", task="读取证据并报告执行限制")
                if not child_id
                else call("followup", "send_message", agent_id=child_id, task="继续核查")
            )
            batch = [operation]
        elif stage == 1:
            batch = [call("wait", "wait_agents", agent_ids=[])]
        else:
            assert outputs(payload)[-1]["agents"][0]["status"] == "completed"
            return httpx.Response(200, json=answer("已收集子任务"))
        return httpx.Response(200, json=tool_response(batch))

    def child(request):
        nonlocal child_count
        child_count += 1
        payload = json.loads(request.content)
        system = payload["messages"][0]["content"]
        assert f"当前执行模式：{expected_mode}。" in system
        assert system.count("当前执行模式：") == 1
        if child_count % 2:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call("read-" + expected_mode, "read_file", path="evidence.txt"),
                        call(
                            "command-" + expected_mode,
                            "run_command",
                            argv=[
                                sys.executable,
                                "-c",
                                f"from pathlib import Path; Path({str(marker)!r}).touch()",
                            ],
                        ),
                    ]
                ),
            )
        results = outputs(payload)[-2:]
        assert results[0]["ok"] and results[0]["lines"][0]["text"] == "evidence"
        assert results[1]["status"] == (
            "mode_blocked" if expected_mode == "plan" else "approval_required"
        )
        assert not marker.exists()
        return httpx.Response(200, json=answer("证据已读，命令没有运行"))

    with application(tmp_path, parent, child) as (app, store):
        app.select_mode("plan")
        app.send("委派调查")
        child_id = json.loads(index_for(app).read_text(encoding="utf-8"))["agents"][0]["agent_id"]
        expected_mode = "act"
        app.select_mode("act")
        app.send("继续同一子任务")
        index = json.loads(index_for(app).read_text(encoding="utf-8"))
        assert [r["execution_mode"] for r in index["agents"][0]["runs"]] == ["plan", "act"]
        children_root = index_for(app).parent / "sessions"
        parent_modes = [
            e["payload"]["context"]["execution_mode"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "request_started"
        ]
        assert parent_modes == ["plan"] * 3 + ["act"] * 3
    with SessionStore(children_root) as children:
        events = children.load_events(child_id)
        child_modes = [
            e["payload"]["context"]["execution_mode"]
            for e in events
            if e["type"] == "request_started"
        ]
        assert child_modes == ["plan"] * 2 + ["act"] * 2
        assert children.load_executions(child_id) == []
    assert child_count == 4 and not marker.exists()
