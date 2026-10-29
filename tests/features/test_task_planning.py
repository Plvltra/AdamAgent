"""E3.2：模型通过真实工具链更新计划，证据来自本地执行与 JSONL。"""

import json
import sys
from contextlib import contextmanager

import httpx
import pytest

from adam.bootstrap import build_application
from adam.contracts.errors import StorageError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def step(status="pending", evidence=None, reason=""):
    return {
        "step_id": "work",
        "content": "check file",
        "status": status,
        "evidence": evidence or [],
        "reason": reason,
    }


@contextmanager
def plan_app(root, handler, *, config=None, notices=None, confirm=None):
    """真实应用和 HTTP 适配；全部文件在临时根，审批由测试显式控制。"""
    config = config or config_at(root)
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            root,
            on_notice=(notices if notices is not None else []).append,
            confirm_command=confirm,
            confirm_edit=confirm,
        )
        try:
            yield app, store
        finally:
            app.close()


@pytest.mark.parametrize("bad_reference", ["invented", "plan1", "oldread", "failedread"])
def test_plan_rejects_fake_self_foreign_or_failed_evidence_then_accepts_real_result(
    tmp_path, bad_reference
):
    (tmp_path / "note.txt").write_text("real evidence", encoding="utf-8")
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        index = len(requests)
        if index == 1:
            data = tool_response([call("oldread", "read_file", path="note.txt")])
        elif index == 2:
            data = answer("previous turn")
        elif index == 3:
            data = tool_response(
                [
                    call("plan1", "update_plan", steps=[step("in_progress")]),
                    call("read", "read_file", path="note.txt"),
                    call("failedread", "read_file", path="missing.txt"),
                ]
            )
        elif index == 4:
            data = tool_response(
                [call("bad", "update_plan", steps=[step("completed", [bad_reference])])]
            )
        elif index == 5:
            assert outputs(body)[-1]["ok"] is False
            data = tool_response([call("good", "update_plan", steps=[step("completed", ["read"])])])
        else:
            assert outputs(body)[-1]["ok"] is True
            data = answer("verified")
        return httpx.Response(200, json=data)

    with plan_app(tmp_path, handler) as (app, store):
        app.send("previous read")
        app.send("check with a plan")
        report = store.load_plan(app.session.session_id)
        assert report.snapshot.version == 2 and report.snapshot.replan_count == 0
        assert report.snapshot.steps[0].evidence == ("read",)
        assert report.turn_status == "succeeded"
        updates = [
            event
            for event in store.load_events(app.session.session_id)
            if event["type"] == "plan_updated"
        ]
        assert [event["payload"]["tool_call_id"] for event in updates] == ["plan1", "good"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_command_exit_code_controls_plan_completion(tmp_path, exit_code):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count == 1:
            data = tool_response(
                [
                    call("plan", "update_plan", steps=[step("in_progress")]),
                    call(
                        "command", "run_command", argv=[sys.executable, "-c", f"exit({exit_code})"]
                    ),
                ]
            )
        elif count == 2:
            # 通用工具 ok=true 不等于命令成功；以独立执行记录中的退出码判断。
            assert outputs(body)[-1]["ok"] is True
            data = tool_response(
                [call("complete", "update_plan", steps=[step("completed", ["command"])])]
            )
        elif count == 3 and exit_code:
            assert outputs(body)[-1]["ok"] is False
            data = tool_response(
                [call("fail", "update_plan", steps=[step("failed", ["command"], "command failed")])]
            )
        else:
            data = answer("command checked")
        return httpx.Response(200, json=data)

    with plan_app(tmp_path, handler, confirm=lambda _: True) as (app, store):
        app.send("execute check")
        plan = store.load_plan(app.session.session_id).snapshot
        assert plan.version == 2
        assert plan.steps[0].status == ("failed" if exit_code else "completed")
        assert store.load_executions(app.session.session_id)[0]["exit_code"] == exit_code


def test_plan_log_failure_stops_later_tools_and_model_requests(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=tool_response(
                [
                    call("plan", "update_plan", steps=[step()]),
                    call("read", "read_file", path="note.txt"),
                ]
            ),
        )

    with plan_app(tmp_path, handler) as (app, store):

        def fail(*args):
            raise StorageError("plan save failed")

        monkeypatch.setattr(store, "append_plan", fail)
        with pytest.raises(StorageError, match="plan save failed"):
            app.send("work")
        events = store.load_events(app.session.session_id)
        assert [
            event["payload"]["name"] for event in events if event["type"] == "tool_started"
        ] == ["update_plan"]
        assert len(requests) == 1
        assert store.load_plan(app.session.session_id) is None
        assert store.load_messages(app.session.session_id) == []
