"""T3.3：跨项目、主子任务、固定正文及真实终端流中的 Skill 协作验收。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

from adam.cli.main import chat
from adam.config.settings import SkillsConfig
from adam.contracts.errors import AgentError
from adam.session.storage import SessionStore
from tests.features.test_explicit_skills import selected_records, skill_application
from tests.features.test_memory_context import config_at
from tests.features.test_standard_skill_metadata import standard
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("explicit", [False, True])
def test_shared_user_library_respects_project_override_and_new_sessions(tmp_path, explicit):
    user_home = tmp_path / "user"
    user_source, _ = standard(user_home, body=b"USER_GUIDANCE")
    project_a, project_b = tmp_path / "a", tmp_path / "b"
    project_source, _ = standard(project_a, body=b"PROJECT_A_GUIDANCE")
    project_b.mkdir()

    def run_project(workspace, source, scope, expected):
        config = replace(config_at(workspace), skills=SkillsConfig(user_dir=user_home / "skills"))
        requests = []
        new_session = False

        def handle(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if new_session:
                encoded = json.dumps(payload)
                assert "USER_GUIDANCE" not in encoded and "PROJECT_A_GUIDANCE" not in encoded
                assert not selected_records(payload) and not outputs(payload)
                return httpx.Response(200, json=answer("普通新会话"))
            if explicit:
                record = selected_records(payload)[0]
                assert record["catalog"]["scope"] == scope
                assert record["catalog"]["source"] == str(source)
                assert record["content"] == expected
            elif not outputs(payload):
                return httpx.Response(
                    200, json=tool_response([call("guide", "load_skill", skill_id="inspect-code")])
                )
            else:
                result = outputs(payload)[-1]
                assert result["ok"] and result["content"] == expected
                assert result["scope"] == scope and result["source"] == str(source)
            if scope == "user":
                assert "PROJECT_A_GUIDANCE" not in json.dumps(payload)
            return httpx.Response(200, json=answer("按所属项目指导完成"))

        with skill_application(workspace, config, handle) as (app, store):
            assert (
                app.send("检查", skill_id="inspect-code" if explicit else None)
                == "按所属项目指导完成"
            )
            first = app.session.session_id
            first_events = store.load_events(first)
            new_session = True
            app.new("普通新会话")
            assert app.send("不需要技能") == "普通新会话"
            assert app.session.session_id != first
            assert store.load_events(first) == first_events
            assert len(requests) == (2 if explicit else 3)

    run_project(project_a, project_source, "project", "PROJECT_A_GUIDANCE")
    run_project(project_b, user_source, "user", "USER_GUIDANCE")


def test_child_loads_user_material_without_parent_guidance_or_extra_permissions(tmp_path):
    workspace, user_home = tmp_path / "project", tmp_path / "user"
    standard(workspace, "parent-only", body=b"PRIVATE_PARENT_GUIDANCE")
    source, _ = standard(user_home, body=b"USER_CHILD_GUIDANCE")
    marker = workspace / "must-not-exist"
    script = source.parent / "scripts" / "check.py"
    script.parent.mkdir()
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')",
        encoding="utf-8",
    )
    (user_home / "outside.md").write_text("OUTSIDE_SECRET", encoding="utf-8")
    config = replace(config_at(tmp_path), skills=SkillsConfig(user_dir=user_home / "skills"))
    parent_requests, child_requests = [], []

    def child(request):
        payload = json.loads(request.content)
        child_requests.append(payload)
        assert "PRIVATE_PARENT_GUIDANCE" not in json.dumps(payload)
        assert not selected_records(payload)
        results = outputs(payload)
        if len(child_requests) == 1:
            assert "USER_CHILD_GUIDANCE" not in json.dumps(payload)
            batch = [call("guide", "load_skill", skill_id="inspect-code")]
        elif len(child_requests) == 2:
            assert results[-1]["content"] == "USER_CHILD_GUIDANCE"
            assert results[-1]["scope"] == "user" and results[-1]["source"] == str(source)
            batch = [
                call("script", "load_skill", skill_id="inspect-code", resource="scripts/check.py")
            ]
        elif len(child_requests) == 3:
            assert results[-1]["content"] == script.read_bytes().decode("utf-8")
            batch = [
                call("outside-tool", "read_file", path=str(script)),
                call(
                    "outside-package",
                    "load_skill",
                    skill_id="inspect-code",
                    resource="../../outside.md",
                ),
                call("execute", "run_command", argv=[sys.executable, str(script)]),
            ]
        else:
            assert [result["ok"] for result in results] == [True, True, False, False, False]
            assert results[-1]["status"] == "approval_required"
            assert "OUTSIDE_SECRET" not in json.dumps(payload)
            assert not marker.exists()
            return httpx.Response(200, json=answer("已读取用户指导，执行需主任务批准"))
        return httpx.Response(200, json=tool_response(batch))

    def parent(request):
        payload = json.loads(request.content)
        parent_requests.append(payload)
        assert selected_records(payload)[0]["content"] == "PRIVATE_PARENT_GUIDANCE"
        if len(parent_requests) == 1:
            batch = [
                call("spawn", "spawn_agent", name="helper", task="读取用户技能及脚本，检查权限")
            ]
        elif len(parent_requests) == 2:
            batch = [call("wait", "wait_agents", agent_ids=[])]
        else:
            assert outputs(payload)[-1]["agents"][0]["status"] == "completed"
            return httpx.Response(200, json=answer("子任务有独立上下文与权限"))
        return httpx.Response(200, json=tool_response(batch))

    with application(workspace, parent, child, config=config) as (app, _):
        assert app.send("委派", skill_id="parent-only") == "子任务有独立上下文与权限"
        children_root = index_for(app).parent / "sessions"
        assert config.permissions.additional_read_dirs == ()
        assert len(child_requests) == 4 and not marker.exists()
    # 任务结束、写者关闭后再独立读取日志，避免测试轮询与 Windows 原子写入争锁。
    with SessionStore(children_root) as children:
        sessions = children.list_sessions()
        assert len(sessions) == 1
        events = children.load_events(sessions[0].session_id)
        requests = [e["payload"] for e in events if e["type"] == "request_started"]
        assert all("selected_skill" not in r["context"] for r in requests)
        assert any(
            entry["scope"] == "user" and entry["source"] == str(source)
            for entry in requests[0]["context"]["skills"]
        )
        loaded = [
            json.loads(e["payload"]["output"]) for e in events if e["type"] == "tool_finished"
        ]
        assert (
            loaded[0]["content"] == "USER_CHILD_GUIDANCE"
            and loaded[1]["resource"] == "scripts/check.py"
        )


def test_explicit_body_stays_fixed_during_turn_and_refreshes_on_next_selection(tmp_path):
    source, header = standard(tmp_path, body=b"GUIDANCE_BEFORE")
    (tmp_path / "evidence.txt").write_text("real evidence", encoding="utf-8")
    snapshots = []

    def handle(request):
        selection = selected_records(json.loads(request.content))[0]
        snapshots.append(selection)
        if len(snapshots) == 1:
            source.write_bytes(header + b"GUIDANCE_AFTER")
            return httpx.Response(
                200, json=tool_response([call("read", "read_file", path="evidence.txt")])
            )
        assert selection["content"] == (
            "GUIDANCE_BEFORE" if len(snapshots) == 2 else "GUIDANCE_AFTER"
        )
        return httpx.Response(200, json=answer("完成"))

    with skill_application(tmp_path, config_at(tmp_path), handle) as (app, store):
        app.send("第一次", skill_id="inspect-code")
        app.send("第二次", skill_id="inspect-code")
        requests = [
            e["payload"]["context"]["selected_skill"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "request_started"
        ]
        assert requests == snapshots
        assert snapshots[0] == snapshots[1]
        assert snapshots[2]["version"] != snapshots[0]["version"]


@pytest.mark.parametrize("failure", ["missing", "oversized"])
def test_failed_project_source_never_falls_back_to_shadowed_user_body(tmp_path, failure):
    workspace, user_home = tmp_path / "project", tmp_path / "user"
    source, header = standard(workspace)
    standard(user_home, body=b"FORBIDDEN_FALLBACK")
    config = replace(config_at(tmp_path), skills=SkillsConfig(user_dir=user_home / "skills"))
    payloads = []

    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        assert "FORBIDDEN_FALLBACK" not in json.dumps(payload)
        if len(payloads) == 1:
            return httpx.Response(
                200, json=tool_response([call("guide", "load_skill", skill_id="inspect-code")])
            )
        assert outputs(payload)[-1]["ok"] is False
        return httpx.Response(200, json=answer("当前项目指导不可用"))

    with skill_application(workspace, config, handle) as (app, store):
        if failure == "missing":
            source.unlink()
        else:
            source.write_bytes(header + b"x" * 9000)
        assert app.send("按技能检查") == "当前项目指导不可用"
        events = store.load_events(app.session.session_id)
        with pytest.raises(AgentError):
            app.send("显式指定同一技能", skill_id="inspect-code")
        assert store.load_events(app.session.session_id) == events
        assert len(payloads) == 2


@pytest.mark.parametrize("cancelled", [False, True])
def test_skill_command_streams_and_cancelled_input_does_not_leak_to_new_session(
    tmp_path, cancelled
):
    standard(tmp_path, body=b"STREAM_GUIDANCE")
    config = config_at(tmp_path)
    config = replace(config, llm=replace(config.llm, stream=True))
    inputs = iter(["/skill inspect-code 本次任务", "/new", "新会话问题", "/exit"])
    payloads, shown, fragments, session_ids = [], [], [], []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        payload = json.loads(request.content)
        payloads.append(payload)
        session_ids.append(app.session.session_id)
        if len(payloads) == 1:
            assert selected_records(payload)[0]["content"] == "STREAM_GUIDANCE"
            parts = [event({"content": "部分"})]
            if cancelled:
                # 在真实 SSE 读取边界注入 Ctrl+C，而不是替换应用或会话保存逻辑。
                parts.append(KeyboardInterrupt())
            else:
                parts.extend([event({"content": "完成"}), event(reason="stop", usage=USAGE), DONE])
        else:
            assert not selected_records(payload) and "STREAM_GUIDANCE" not in json.dumps(payload)
            parts = [event({"content": "新回答"}), event(reason="stop", usage=USAGE), DONE]
        return sse(parts)

    with skill_application(tmp_path, config, handle) as (app, store):
        chat(app, read=lambda _: next(inputs), write=shown.append, fragment=fragments.append)
        assert len(payloads) == 2 and session_ids[0] != session_ids[1]
        assert "部分" in fragments and "新回答" in fragments
        old_turns = store.load_turns(session_ids[0])
        assert len(old_turns) == (0 if cancelled else 1)
        assert any("已取消本轮" in line for line in shown) is cancelled
        assert app.history()[-1].content == "新回答"
        assert not any(line.endswith("部分完成") for line in shown)


def test_automatic_compaction_preserves_explicit_body_in_following_chat_request(tmp_path):
    standard(tmp_path, body=b"PROTECTED_GUIDANCE")
    config = config_at(tmp_path)
    with skill_application(tmp_path, config, lambda _: pytest.fail("preview must not request")) as (
        app,
        _,
    ):
        baseline = app.preview_context().estimate.input_tokens
    config = replace(
        config,
        context=replace(
            config.context,
            max_input_tokens=baseline + 5000,
            safety_margin_tokens=100,
            auto_compact=True,
        ),
    )
    payloads = []

    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if "tools" not in payload:
            assert not selected_records(payload)
            return httpx.Response(200, json=answer("旧任务约束与进度摘要"))
        assert selected_records(payload)[0]["content"] == "PROTECTED_GUIDANCE"
        assert any("旧任务约束与进度摘要" in str(m.get("content")) for m in payload["messages"])
        return httpx.Response(200, json=answer("按本轮指导继续"))

    with skill_application(tmp_path, config, handle) as (app, store):
        sid = app.new("长会话").session_id
        for index in range(8):
            store.append_turn(sid, f"旧问题 {index}", "中" * 1000)
        assert app.send("继续", skill_id="inspect-code") == "按本轮指导继续"
        assert len(payloads) == 2 and "tools" not in payloads[0] and "tools" in payloads[1]
        assert store.load_summary(sid) is not None
        assert len(store.load_turns(sid)) == 9
