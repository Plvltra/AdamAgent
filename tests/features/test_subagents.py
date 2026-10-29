"""V6 委派业务：假 HTTP 经过真实模型适配、并发任务、文件工具及 JSONL 保存。"""

import json
import threading
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import AgentConfig, AppConfig, LLMConfig, ReflectionConfig, SubagentConfig
from adam.contracts.errors import BudgetError, FailureCode
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_tool_loop import answer, tool_response


def call(call_id, tool_name, **arguments):
    return call_id, tool_name, json.dumps(arguments)


def outputs(body):
    return [json.loads(item["content"]) for item in body["messages"] if item["role"] == "tool"]


@contextmanager
def application(tmp_path, parent_handler, child_handler, *, config=None, notices=None):
    """每个子模型在所属线程创建独立 HTTP；所有写入落在测试临时目录。"""
    config = config or AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "ROOT_CONSTRAINT: preserve evidence",
        agent=AgentConfig(max_session_tokens=200000),
        reflection=ReflectionConfig(enabled=False),
    )

    def reject_approval(_):
        pytest.fail("a delegated operation must not ask for interactive approval")

    @contextmanager
    def child_client(cancel):
        with httpx.Client(transport=httpx.MockTransport(child_handler)) as http:
            yield LLMClient(config.llm, http)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(parent_handler)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            tmp_path,
            child_client_factory=child_client,
            confirm_command=reject_approval,
            confirm_edit=reject_approval,
            on_notice=(notices if notices is not None else []).append,
        )
        try:
            yield app, store
        finally:
            app.close()


def index_for(app):
    return app.sessions_dir / "_subagents" / app.session.session_id / "index.json"


def test_two_children_overlap_use_tools_and_parent_collects_results(tmp_path):
    (tmp_path / "a.txt").write_text("evidence A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("evidence B", encoding="utf-8")
    both = threading.Barrier(2)
    ready, release = threading.Event(), threading.Event()
    requests, threads, parent_requests = [], set(), []
    notices = []

    def child(request):
        body = json.loads(request.content)
        requests.append(body)
        threads.add(threading.get_ident())
        assert "ROOT_CONSTRAINT" in body["messages"][0]["content"]
        assert "parent-only background" not in json.dumps(body)
        names = {item["function"]["name"] for item in body["tools"]}
        assert not {"spawn_agent", "send_message", "wait_agents"} & names
        if not outputs(body):
            both.wait(timeout=10)
            ready.set()
            assert release.wait(10)
            task = next(item["content"] for item in body["messages"] if item["role"] == "user")
            return httpx.Response(200, json=tool_response([call("read", "read_file", path=task)]))
        return httpx.Response(200, json=answer("verified " + body["messages"][1]["content"]))

    def parent(request):
        body = json.loads(request.content)
        parent_requests.append(body)
        if len(parent_requests) == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call("a", "spawn_agent", name="A", task="a.txt"),
                        call("b", "spawn_agent", name="B", task="b.txt"),
                    ]
                ),
            )
        if len(parent_requests) == 2:
            # 两个子请求已同时进入；此时主 Agent 的下一次请求也已开始。
            assert ready.wait(10)
            release.set()
            return httpx.Response(
                200, json=tool_response([call("wait", "wait_agents", agent_ids=[])])
            )
        results = outputs(body)[-1]["agents"]
        assert {item["reply"] for item in results} == {"verified a.txt", "verified b.txt"}
        assert {item["status"] for item in results} == {"completed"}
        return httpx.Response(200, json=answer("combined evidence"))

    try:
        with application(tmp_path, parent, child, notices=notices) as (app, store):
            assert app.send("analyze both files; parent-only background") == "combined evidence"
            assert app.usage().tokens == 7 * 12
            assert not app.usage().unknown
            assert len(threads) == 2 and threading.get_ident() not in threads
            assert len(app.list_sessions()) == 1
            index = json.loads(index_for(app).read_text(encoding="utf-8"))
            parent_events = store.load_events(app.session.session_id)
            calls = {
                item["payload"]["tool_call_id"]: item["payload"]
                for item in parent_events
                if item["type"] == "tool_started"
            }
            for agent in index["agents"]:
                run = agent["runs"][-1]
                assert run["parent_turn_id"] == calls[run["tool_call_id"]]["turn_id"]
                assert run["request_id"] == calls[run["tool_call_id"]]["request_id"]
                assert run["child_turn_id"]
            assert sum("已启动" in line for line in notices) == 2
            assert sum("completed" in line for line in notices) == 2
    finally:
        release.set()


def test_child_permissions_do_not_prompt_write_execute_or_redelegate(tmp_path):
    original = tmp_path / "safe.txt"
    original.write_text("original", encoding="utf-8")
    marker = tmp_path / "must-not-exist"
    parent_count = 0
    child_bodies = []

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        if parent_count == 1:
            data = tool_response(
                [call("spawn", "spawn_agent", name="helper", task="analyze permissions")]
            )
        elif parent_count == 2:
            data = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(json.loads(request.content))[-1]["agents"][0]["status"] == "completed"
            data = answer("main agent must handle changes")
        return httpx.Response(200, json=data)

    def child(request):
        body = json.loads(request.content)
        child_bodies.append(body)
        if len(child_bodies) == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call(
                            "command",
                            "run_command",
                            argv=["python", "-c", f"open({str(marker)!r},'w').close()"],
                        ),
                        call(
                            "edit",
                            "replace_text",
                            path="safe.txt",
                            old_text="original",
                            new_text="bad",
                            expected_version="v",
                        ),
                        call("nested", "spawn_agent", name="nested", task="forbidden"),
                    ]
                ),
            )
        results = outputs(body)
        assert [item["status"] for item in results[:2]] == [
            "approval_required",
            "approval_required",
        ]
        assert results[2]["ok"] is False
        return httpx.Response(200, json=answer("approval required; proposal only"))

    with application(tmp_path, parent, child) as (app, _):
        app.send("delegate analysis")
        assert original.read_text(encoding="utf-8") == "original"
        assert not marker.exists()
        assert len(json.loads(index_for(app).read_text(encoding="utf-8"))["agents"]) == 1


def test_child_can_resume_after_restart_without_automatic_requests(tmp_path):
    child_id, session_id = None, None
    child_bodies = []

    def child(request):
        body = json.loads(request.content)
        child_bodies.append(body)
        return httpx.Response(
            200, json=answer("first finding" if len(child_bodies) == 1 else "followup finding")
        )

    def run_parent(reuse):
        step = 0

        def parent(request):
            nonlocal step
            step += 1
            if step == 1:
                tool = (
                    call("resume", "send_message", agent_id=child_id, task="continue finding")
                    if reuse
                    else call("new", "spawn_agent", name="reusable", task="initial finding")
                )
                result = tool_response([tool])
            elif step == 2:
                result = tool_response([call("wait", "wait_agents", agent_ids=[])])
            else:
                result = answer("summary")
            return httpx.Response(200, json=result)

        return parent

    with application(tmp_path, run_parent(False), child) as (app, _):
        app.send("first")
        session_id = app.session.session_id
        child_id = json.loads(index_for(app).read_text(encoding="utf-8"))["agents"][0]["agent_id"]
    with application(tmp_path, run_parent(True), child) as (app, _):
        app.resume(session_id)
        assert len(child_bodies) == 1
        assert app.usage().tokens == 4 * 12
        assert len(child_bodies) == 1
        app.send("follow up with the same helper")
        assert app.usage().tokens == 8 * 12
        messages = child_bodies[-1]["messages"]
        assert [item["content"] for item in messages if item["role"] == "user"] == [
            "initial finding",
            "continue finding",
        ]
        assert any(item.get("content") == "first finding" for item in messages)
        record = json.loads(index_for(app).read_text(encoding="utf-8"))["agents"][0]
        assert len(record["runs"]) == 2
        assert record["runs"][0]["parent_turn_id"] != record["runs"][1]["parent_turn_id"]


def test_parent_cannot_commit_answer_before_collecting_child_result(tmp_path):
    requests, displayed = [], []

    def child(request):
        return httpx.Response(200, json=answer("child evidence"))

    def parent(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            result = tool_response([call("spawn", "spawn_agent", name="helper", task="check")])
        elif len(requests) == 2:
            result = answer("premature answer")
        elif len(requests) == 3:
            assert "尚未收集" in body["messages"][0]["content"]
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            result = answer("answer with evidence")
        return httpx.Response(200, json=result)

    with application(tmp_path, parent, child) as (app, _):
        assert app.send("check", on_output=displayed.append) == "answer with evidence"
        assert "premature answer" not in str([message.raw for message in app.history()])
        assert app.usage().tokens == 5 * 12


def test_capacity_and_new_creation_limits_are_enforced(tmp_path):
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        agent=AgentConfig(max_session_tokens=200000),
        reflection=ReflectionConfig(enabled=False),
        subagents=SubagentConfig(max_concurrent=1, max_created_per_turn=1),
    )
    release = threading.Event()
    count = 0

    def child(request):
        assert release.wait(10)
        return httpx.Response(200, json=answer("done"))

    def parent(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count == 1:
            result = tool_response(
                [
                    call("first", "spawn_agent", name="one", task="work"),
                    call("second", "spawn_agent", name="two", task="work"),
                ]
            )
        elif count == 2:
            assert outputs(body)[-1]["ok"] is False
            assert "并发" in outputs(body)[-1]["error"]
            release.set()
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        elif count == 3:
            result = tool_response([call("third", "spawn_agent", name="three", task="work")])
        else:
            assert "新建" in outputs(body)[-1]["error"]
            result = answer("one helper only")
        return httpx.Response(200, json=result)

    try:
        with application(tmp_path, parent, child, config=config) as (app, _):
            assert app.send("limited work") == "one helper only"
    finally:
        release.set()


def test_unknown_child_usage_blocks_new_group_requests_and_survives_restart(tmp_path):
    count = 0

    def child(request):
        return httpx.Response(503, json={"error": "unavailable"})

    def parent(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                200, json=tool_response([call("spawn", "spawn_agent", name="bad", task="fail")])
            )
        return httpx.Response(200, json=tool_response([call("wait", "wait_agents", agent_ids=[])]))

    with application(tmp_path, parent, child) as (app, _):
        with pytest.raises(BudgetError) as caught:
            app.send("delegate")
        assert caught.value.code == FailureCode.USAGE_UNKNOWN
        session_id = app.session.session_id
        assert app.usage().unknown
    with application(tmp_path, parent, child) as (app, _):
        app.resume(session_id)
        before = count
        with pytest.raises(BudgetError):
            app.send("do not retry")
        assert count == before
        assert app.usage().unknown


def test_estimated_group_admission_rejects_before_request_is_logged(tmp_path):
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        agent=replace(AgentConfig(), max_session_tokens=10),
    )

    def unexpected(request):
        pytest.fail("no HTTP request should be sent")

    with application(tmp_path, unexpected, unexpected, config=config) as (app, store):
        with pytest.raises(BudgetError):
            app.send("too little remaining budget")
        events = store.load_events(app.session.session_id)
        assert not any(item["type"] == "request_started" for item in events)
        assert not app.usage().unknown
