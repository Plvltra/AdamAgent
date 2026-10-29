"""委派边界：共享摘要消费、可靠索引、结果大小和主会话隔离。"""

import json
import time
from dataclasses import replace

import httpx
import pytest

import adam.session.delegation as delegation_storage
from adam.config.settings import (
    AgentConfig,
    AppConfig,
    ContextConfig,
    LLMConfig,
    PermissionConfig,
    ToolLimits,
)
from adam.contracts.errors import BudgetError, StorageError
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


def settings(tmp_path, **changes):
    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        agent=AgentConfig(max_session_tokens=200000),
        context=ContextConfig(auto_compact=False, retain_recent_turns=0),
    )
    return replace(config, **changes)


def test_shared_usage_includes_child_retry_and_parent_compaction(tmp_path):
    parent_count, child_count = 0, 0

    def child(request):
        nonlocal child_count
        child_count += 1
        if child_count < 3:
            raise httpx.ConnectError("test connection not established", request=request)
        return httpx.Response(200, json=answer("child result"))

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        if parent_count == 1:
            result = tool_response([call("spawn", "spawn_agent", name="helper", task="inspect")])
        elif parent_count == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            result = answer("summary or answer")
        return httpx.Response(200, json=result)

    config = settings(tmp_path)
    with application(tmp_path, parent, child, config=config) as (app, store):
        app.send("delegate")
        app.send("next")
        app.send("next again")
        assert app.compact() is not None
        assert child_count == 3 and parent_count == 6
        assert store.usage(app.session.session_id) == (6 * 12, False)
        assert app.usage().tokens == 7 * 12
        assert not app.usage().unknown
        child_root = index_for(app).parent / "sessions"
        paths = list(child_root.glob("*/*/*/*.jsonl"))
        events = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
        assert sum(item["type"] == "request_started" for item in events) == 3


def test_dispatch_index_failure_preserves_previous_snapshot_and_sends_no_child(
    tmp_path, monkeypatch
):
    original_replace = delegation_storage.os.replace
    child_requests, snapshots = [], []

    def fail_run_publish(source, destination):
        if destination.name == "index.json":
            data = json.loads(source.read_text(encoding="utf-8"))
            if any(agent["runs"] for agent in data["agents"]):
                snapshots.append(destination.read_bytes())
                raise OSError("injected index replacement failure")
        return original_replace(source, destination)

    def child(request):
        child_requests.append(request)
        return httpx.Response(200, json=answer("must not run"))

    def parent(request):
        return httpx.Response(
            200, json=tool_response([call("spawn", "spawn_agent", name="helper", task="work")])
        )

    with application(tmp_path, parent, child) as (app, _):
        with monkeypatch.context() as patch:
            patch.setattr(delegation_storage.os, "replace", fail_run_publish)
            with pytest.raises(StorageError):
                app.send("delegate")
        assert child_requests == []
        assert index_for(app).read_bytes() == snapshots[0]
        assert json.loads(snapshots[0])["agents"][0]["runs"] == []
        assert app.history() == []
        assert app.usage().tokens == 12
        assert not app.usage().unknown


def test_result_json_obeys_limit_without_losing_child_identity(tmp_path):
    count = 0

    def child(request):
        return httpx.Response(200, json=answer("evidence\n" * 1000))

    def parent(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count == 1:
            result = tool_response([call("spawn", "spawn_agent", name="helper", task="inspect")])
        elif count == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            raw = [item["content"] for item in body["messages"] if item["role"] == "tool"][-1]
            assert len(raw) <= 512
            received = outputs(body)[-1]["agents"][0]
            assert received["agent_id"] == outputs(body)[0]["agent_id"]
            assert received["status"] == "completed" and received["truncated"]
            assert received["reply"]
            result = answer("bounded evidence")
        return httpx.Response(200, json=result)

    config = settings(tmp_path, tool_limits=ToolLimits(max_result_chars=512))
    with application(tmp_path, parent, child, config=config) as (app, _):
        assert app.send("delegate") == "bounded evidence"
        assert (
            len(
                json.loads(index_for(app).read_text(encoding="utf-8"))["agents"][0]["runs"][0][
                    "reply"
                ]
            )
            > 512
        )


def test_switching_main_session_does_not_leak_pending_child_ids_into_preview(tmp_path):
    count = 0

    def child(request):
        return httpx.Response(200, json=answer("finding"))

    def parent(request):
        nonlocal count
        count += 1
        result = (
            tool_response([call("spawn", "spawn_agent", name="helper", task="inspect")])
            if count == 1
            else answer("premature")
        )
        return httpx.Response(200, json=result)

    config = settings(tmp_path, agent=AgentConfig(max_session_tokens=200000, max_model_requests=3))
    with application(tmp_path, parent, child, config=config) as (app, _):
        with pytest.raises(BudgetError):
            app.send("must collect")
        agent_id = json.loads(index_for(app).read_text(encoding="utf-8"))["agents"][0]["agent_id"]
        app.new("another main session")
        preview = app.preview_context()
        assert agent_id not in json.dumps(preview.messages)
        assert preview.messages[0] == {"role": "system", "content": "system"}


def test_child_read_scope_inherits_only_authorized_directories(tmp_path):
    workspace, allowed, private = [tmp_path / name for name in ("workspace", "allowed", "private")]
    for directory in (workspace, allowed, private):
        directory.mkdir()
        (directory / "item.txt").write_text(directory.name, encoding="utf-8")
    parent_count, child_count = 0, 0

    def child(request):
        nonlocal child_count
        child_count += 1
        if child_count == 1:
            result = tool_response(
                [
                    call("allowed", "read_file", path=str(allowed / "item.txt")),
                    call("private", "read_file", path=str(private / "item.txt")),
                ]
            )
        else:
            results = outputs(json.loads(request.content))
            assert results[0]["ok"] is True and results[1]["ok"] is False
            result = answer("authorized scope only")
        return httpx.Response(200, json=result)

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        if parent_count == 1:
            result = tool_response(
                [call("spawn", "spawn_agent", name="helper", task="inspect scope")]
            )
        elif parent_count == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert (
                outputs(json.loads(request.content))[-1]["agents"][0]["reply"]
                == "authorized scope only"
            )
            result = answer("done")
        return httpx.Response(200, json=result)

    config = settings(tmp_path, permissions=PermissionConfig(additional_read_dirs=(allowed,)))
    with application(workspace, parent, child, config=config) as (app, _):
        assert app.send("check scopes") == "done"


def test_missing_index_does_not_silently_erase_child_consumption(tmp_path):
    calls = []

    def child(request):
        return httpx.Response(200, json=answer("evidence"))

    def parent(request):
        calls.append(request)
        if len(calls) == 1:
            result = tool_response([call("spawn", "spawn_agent", name="helper", task="inspect")])
        elif len(calls) == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            result = answer("done")
        return httpx.Response(200, json=result)

    with application(tmp_path, parent, child) as (app, _):
        app.send("work")
        session_id, index = app.session.session_id, index_for(app)
    index.unlink()  # 模拟仅复制 JSONL 而遗漏委派索引的错误备份。
    before = len(calls)
    with application(tmp_path, parent, child) as (app, _):
        app.resume(session_id)
        with pytest.raises(StorageError):
            app.usage()
        assert len(calls) == before


def test_followup_cannot_replace_uncollected_result_in_same_turn(tmp_path):
    count, child_calls = 0, []
    active_app = []

    def child(request):
        child_calls.append(request)
        return httpx.Response(200, json=answer("finding"))

    def parent(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        if count == 1:
            result = tool_response([call("spawn", "spawn_agent", name="helper", task="first")])
        else:
            agent_id = outputs(body)[0]["agent_id"]
            if count == 2:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        record = json.loads(index_for(active_app[0]).read_text(encoding="utf-8"))
                    except PermissionError:
                        # 外部测试观察者不持有索引线程锁；Windows 原子替换时短暂拒绝读。
                        time.sleep(0.01)
                        continue
                    if record["agents"][0]["runs"][-1]["status"] == "completed":
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("child did not finish within the observation deadline")
                result = tool_response(
                    [call("early", "send_message", agent_id=agent_id, task="second")]
                )
            elif count == 3:
                assert "wait_agents" in outputs(body)[-1]["error"]
                result = tool_response([call("first-result", "wait_agents", agent_ids=[agent_id])])
            elif count == 4:
                result = tool_response(
                    [call("continue", "send_message", agent_id=agent_id, task="second")]
                )
            elif count == 5:
                result = tool_response([call("second-result", "wait_agents", agent_ids=[agent_id])])
            else:
                result = answer("both results collected")
        return httpx.Response(200, json=result)

    with application(tmp_path, parent, child) as (app, _):
        active_app.append(app)
        assert app.send("work in two steps") == "both results collected"
        assert len(child_calls) == 2
        runs = json.loads(index_for(app).read_text(encoding="utf-8"))["agents"][0]["runs"]
        assert len(runs) == 2 and runs[0]["parent_turn_id"] == runs[1]["parent_turn_id"]
