"""T12.3：同任务开销对照及按需发现与既有能力的组合验收。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

from adam.config.hooks import HooksConfig
from adam.config.mcp import MCPConfig, MCPServerConfig
from adam.config.settings import ToolDiscoveryConfig
from adam.contracts.errors import BudgetError
from tests.features.test_hook_process import checker
from tests.features.test_mcp_connection import SERVICE, assert_stopped
from tests.features.test_standard_skill_metadata import standard
from tests.features.test_subagents import application as delegated_application
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_discovery_loop import application, names, settings
from tests.features.test_tool_loop import answer, tool_response


def test_same_read_task_full_and_demand_compare_schema_and_request_cost(tmp_path):
    results = {}
    for mode in ("full", "on_demand"):
        root = tmp_path / mode
        root.mkdir()
        (root / "note.txt").write_text("value=42", encoding="utf-8")

        def respond(body, number, mode=mode):
            if mode == "on_demand" and number == 1:
                return tool_response([call("discover", "discover_tools", names=["read_file"])])
            if not any("lines" in item for item in outputs(body)):
                return tool_response([call("read", "read_file", path="note.txt")])
            assert outputs(body)[-1]["lines"][0]["text"] == "value=42"
            return answer("value=42")

        config = settings(root, tool_discovery=ToolDiscoveryConfig(mode=mode))
        with application(root, respond, config=config) as (app, store, bodies):
            assert app.send("读取 note.txt，报告 value") == "value=42"
            records = [
                e["payload"]["context"]
                for e in store.load_events(app.session.session_id)
                if e["type"] == "request_started"
            ]
            results[mode] = {
                "requests": len(bodies),
                "initial_tools": len(names(bodies[0])),
                "initial_schema_bytes": records[0]["tool_schema"]["utf8_bytes"],
                "total_schema_bytes": sum(r["tool_schema"]["utf8_bytes"] for r in records),
                "estimated_total_input": sum(r["estimated_input_tokens"] for r in records),
            }
    assert results["on_demand"]["requests"] == 3 and results["full"]["requests"] == 2
    assert results["on_demand"]["initial_tools"] == 2 < results["full"]["initial_tools"]
    assert results["on_demand"]["total_schema_bytes"] < results["full"]["total_schema_bytes"]
    assert results["on_demand"]["estimated_total_input"] < results["full"]["estimated_total_input"]
    # 固定假模型仅证明相同本地任务和输入开销；不把假 usage 当真实消费或模型选择效果。
    print("T12_COMPARISON=" + json.dumps(results, ensure_ascii=False))


def test_skill_and_real_mcp_discover_then_execute_with_original_schema(tmp_path):
    standard(tmp_path, body=b"SKILL_BODY_EVIDENCE")
    pid_file = tmp_path / "server.pid"
    config = settings(
        tmp_path,
        mcp=MCPConfig(
            True,
            (
                MCPServerConfig(
                    "sample",
                    sys.executable,
                    (str(SERVICE), "--pid-file", str(pid_file)),
                    ("probe",),
                ),
            ),
        ),
    )

    def respond(body, number):
        if number == 1:
            assert names(body) == ["list_tools", "discover_tools"]
            return tool_response(
                [
                    call("list-mcp", "list_tools", group="mcp"),
                    call("list-skills", "list_tools", group="skills"),
                ]
            )
        if number == 2:
            assert names(body) == ["list_tools", "discover_tools"]
            pages = outputs(body)
            assert pages[0]["tools"][0]["name"] == "mcp_sample_probe"
            assert pages[1]["tools"][0]["name"] == "load_skill"
            assert not pid_file.with_suffix(".calls").exists()
            return tool_response([call("discover", "discover_tools", groups=["mcp", "skills"])])
        if number == 3:
            assert set(names(body)) == {
                "list_tools",
                "discover_tools",
                "load_skill",
                "mcp_sample_probe",
            }
            return tool_response(
                [
                    call("skill", "load_skill", skill_id="inspect-code"),
                    call("bad-mcp", "mcp_sample_probe", data={"value": "wrong"}),
                    call("mcp", "mcp_sample_probe", data={"value": 42}),
                ]
            )
        result = outputs(body)[-3:]
        assert result[0]["content"] == "SKILL_BODY_EVIDENCE"
        assert not result[1]["ok"] and "Schema" in result[1]["error"]
        assert result[2]["structured_content"]["data"] == {"value": 42}
        return answer("skill + MCP")

    with application(tmp_path, respond, config=config) as (app, store, _):
        assert app.send("按技能查询 MCP") == "skill + MCP"
        assert pid_file.with_suffix(".calls").read_text(encoding="utf-8").splitlines() == ["ok"]
        events = store.load_events(app.session.session_id)
        actual = [e["payload"] for e in events if e["type"] == "request_started"]
        assert all(r["context"]["skills"][0]["id"] == "inspect-code" for r in actual)
    assert_stopped(int(pid_file.read_text(encoding="utf-8")))


def test_new_resume_and_reopen_never_restore_tool_selection(tmp_path):
    (tmp_path / "note.txt").write_text("42", encoding="utf-8")
    phase = "first"

    def respond(body, number):
        if phase == "first":
            if number == 1:
                return tool_response([call("discover", "discover_tools", names=["read_file"])])
            if number == 2:
                return tool_response([call("read", "read_file", path="note.txt")])
            return answer()
        assert names(body) == ["list_tools", "discover_tools"]
        return answer()

    with application(tmp_path, respond) as (app, _, bodies):
        app.send("first")
        first = app.session.session_id
        phase = "later"
        app.new("second")
        app.send("second")
        app.resume(first)
        app.send("resumed")
        assert len(bodies) == 5
    with application(tmp_path, respond) as (app, _, bodies):
        app.resume(first)
        assert bodies == []  # 重开日志不发送请求或重跑发现。
        app.send("reopened")
        assert len(bodies) == 1


def test_expanded_schema_stops_at_original_input_budget_before_next_http(tmp_path):
    def respond(body, number):
        assert number == 1
        return tool_response(
            [
                call(
                    "discover",
                    "discover_tools",
                    groups=["files", "git", "editing", "code", "commands"],
                )
            ]
        )

    # 用实际首请求校准，基础预览不包含执行时的动态状态；不能把首请求失败当作扩展拦截。
    with application(tmp_path, lambda *_: answer("baseline")) as (app, store, _):
        app.send("task")
        initial = next(
            e["payload"]["context"]["estimated_input_tokens"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "request_started"
        )
    config = settings(tmp_path)
    config = replace(
        config,
        context=replace(
            config.context,
            max_input_tokens=initial + 100,
            safety_margin_tokens=0,
        ),
    )
    with application(tmp_path, respond, config=config) as (app, store, bodies):
        with pytest.raises(BudgetError):
            app.send("task")
        assert len(bodies) == 1
        events = store.load_events(app.session.session_id)
        assert [e["payload"]["name"] for e in events if e["type"] == "tool_started"] == [
            "discover_tools",
        ]


def test_discovery_does_not_grant_outside_workspace_read(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("DO_NOT_READ", encoding="utf-8")

    def respond(body, number):
        if number == 1:
            return tool_response([call("discover", "discover_tools", names=["read_file"])])
        if number == 2:
            return tool_response([call("outside", "read_file", path=str(outside))])
        assert not outputs(body)[-1]["ok"]
        assert "DO_NOT_READ" not in json.dumps(body)
        return answer("无权限")

    with application(root, respond) as (app, _, _):
        assert app.send("read outside") == "无权限"


def test_discovered_read_still_obeys_original_hook(tmp_path):
    (tmp_path / "note.txt").write_text("evidence", encoding="utf-8")
    program = checker(
        tmp_path,
        "import json, sys\nfrom pathlib import Path\n"
        "data=json.load(sys.stdin)\n"
        "name=data['tool']['name']\n"
        "Path(name+'.hook.json').write_text(json.dumps(data), encoding='utf-8')\n"
        "print(json.dumps({'version':1,'decision':'deny' if name=='read_file' else 'allow',"
        "'reason':'local policy'}))\n",
    )
    hooks = HooksConfig(enabled=True, before_tool=program)

    def respond(body, number):
        if number == 1:
            return tool_response([call("discover", "discover_tools", names=["read_file"])])
        if number == 2:
            assert "read_file" in names(body)
            return tool_response([call("read", "read_file", path="note.txt")])
        assert outputs(body)[-1]["status"] == "hook_blocked"
        assert not any("lines" in item for item in outputs(body))
        return answer("本地策略拒绝")

    with application(tmp_path, respond, config=settings(tmp_path, hooks=hooks)) as (app, store, _):
        assert app.send("read") == "本地策略拒绝"
        events = store.load_events(app.session.session_id)
        assert [e["payload"]["name"] for e in events if e["type"] == "tool_started"] == [
            "discover_tools",
            "read_file",
        ]
    assert (tmp_path / "discover_tools.hook.json").exists()
    assert (tmp_path / "read_file.hook.json").exists()


def test_parent_child_discovery_is_independent_and_child_permission_stays_limited(tmp_path):
    (tmp_path / "note.txt").write_text("child evidence", encoding="utf-8")
    parent_bodies, child_bodies = [], []

    def parent(request):
        body = json.loads(request.content)
        parent_bodies.append(body)
        count = len(parent_bodies)
        if count == 1:
            batch = [call("discover", "discover_tools", groups=["agents"], names=["read_file"])]
        elif count == 2:
            batch = [call("spawn", "spawn_agent", name="helper", task="读 note.txt 并说明权限")]
        elif count == 3:
            batch = [call("wait", "wait_agents", agent_ids=[])]
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            return httpx.Response(200, json=answer("collected"))
        return httpx.Response(200, json=tool_response(batch))

    def child(request):
        body = json.loads(request.content)
        child_bodies.append(body)
        count = len(child_bodies)
        if count == 1:
            assert names(body) == ["list_tools", "discover_tools"]
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call(
                            "discover-child",
                            "discover_tools",
                            names=["read_file", "run_command"],
                        )
                    ]
                ),
            )
        if count == 2:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call("read", "read_file", path="note.txt"),
                        call("command", "run_command", argv=[sys.executable, "-c", "print('NO')"]),
                    ]
                ),
            )
        assert outputs(body)[-2]["lines"][0]["text"] == "child evidence"
        assert outputs(body)[-1]["status"] == "approval_required"
        return httpx.Response(200, json=answer("read only"))

    config = settings(tmp_path)
    config = replace(config, agent=replace(config.agent, max_session_tokens=200000))
    with delegated_application(tmp_path, parent, child, config=config) as (app, _):
        assert app.send("delegate") == "collected"
        assert len(parent_bodies) == 4 and len(child_bodies) == 3
        assert app.usage().tokens == 7 * 12
