"""C00.1：可选能力经过原主子工具管线，资源按调用回收，不依赖真实模型。"""

import builtins
import json
import threading

import httpx
import pytest

import adam.web.adapters.http as web_http
import adam.web.runtime as web_runtime
from adam.config.settings import AgentConfig, ToolDiscoveryConfig, ToolLimits, WebConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import BudgetError, RequestInterrupted
from adam.contracts.execution import ToolCapability
from adam.recording.runtime import replay_turn
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.web_tools import register_web
from adam.web.models import FetchLimits
from adam.web.policy import NetworkPolicy
from tests.features.test_subagents import application as delegated_application
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_discovery_loop import application, names, settings
from tests.features.test_tool_hooks import hooks_at
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


@pytest.fixture
def local_web(website, monkeypatch):
    """只替换可信网络策略以放行测试 origin；HTTP 和资源关闭仍运行生产实现。"""
    origin, requests, ready, release = website
    closed = []
    original_open = web_runtime.open_reader
    original_close = web_http.HttpPageTransport.close

    def open_local(limits, policy):
        return original_open(limits, NetworkPolicy(frozenset({origin})))

    def close(transport):
        closed.append((transport, threading.get_ident()))
        return original_close(transport)

    monkeypatch.setattr(web_runtime, "open_reader", open_local)
    monkeypatch.setattr(web_http.HttpPageTransport, "close", close)
    return origin, requests, ready, release, closed


def configured(root, **changes):
    """固定工程验收配置，仅启用当前实际使用的网页能力。"""
    return settings(root, web=WebConfig(enabled=True), **changes)


def test_optional_read_discovery_and_source_survive_restart(tmp_path, local_web):
    origin, requests, _, _, closed = local_web

    def respond(body, number):
        if number == 1:
            assert names(body) == ["list_tools", "discover_tools"]
            return tool_response([call("discover", "discover_tools", names=["read_webpage"])])
        if number == 2:
            assert "read_webpage" in names(body)
            return tool_response([call("read", "read_webpage", url=origin + "/redirect")])
        result = outputs(body)[-1]
        assert result["ok"] and result["source"]["text"] == "Report Version one"
        assert result["source"]["final_url"] == origin + "/page"
        return answer("observed a versioned public page")

    config = configured(tmp_path)
    with application(tmp_path, respond, config=config) as (app, store, _):
        assert app.send("查网页") == "observed a versioned public page"
        sid = app.session.session_id
        events = store.load_events(sid)
        assert any(
            e["type"] == "tool_started" and e["payload"]["name"] == "read_webpage" for e in events
        )
        assert any("sha256" in json.dumps(e) and "Version one" in json.dumps(e) for e in events)
    with SessionStore(config.sessions_dir) as restored:
        assert restored.load_events(sid) == events
    assert [r[0] for r in requests] == ["/redirect", "/page"]
    assert len(closed) == 1 and closed[0][1] != threading.get_ident()


def test_disabled_capability_chats_without_importing_web_backend(tmp_path, monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("adam.web"):
            raise AssertionError("disabled capability imported web implementation")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)

    def respond(body, number):
        assert "read_webpage" not in names(body)
        return answer("ordinary text")

    config = settings(tmp_path, tool_discovery=ToolDiscoveryConfig(mode="full"))
    with application(tmp_path, respond, config=config) as (app, _, _):
        assert app.send("hello") == "ordinary text"


def test_enabled_chat_does_not_construct_unused_resources(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ordinary chat created web resources")

    monkeypatch.setattr(web_runtime, "HttpPageTransport", forbidden)
    config = configured(tmp_path, tool_discovery=ToolDiscoveryConfig(mode="full"))
    with application(tmp_path, lambda *_: answer("no lookup needed"), config=config) as (app, _, _):
        assert app.send("hello") == "no lookup needed"


def test_tool_budget_stops_whole_batch_before_any_network(tmp_path, local_web):
    origin, requests, _, _, closed = local_web
    config = configured(
        tmp_path,
        agent=AgentConfig(max_tool_calls=1),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    response = tool_response(
        [
            call("one", "read_webpage", url=origin + "/page"),
            call("two", "read_webpage", url=origin + "/page"),
        ]
    )
    with application(tmp_path, lambda *_: response, config=config) as (app, _, _):
        with pytest.raises(BudgetError):
            app.send("read twice")
    assert requests == [] and closed == []


def test_existing_hook_can_deny_web_read_without_allocating_network(tmp_path, local_web):
    origin, requests, _, _, closed = local_web
    config = configured(
        tmp_path,
        hooks=hooks_at(tmp_path, before="deny"),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )

    def respond(body, number):
        if number == 1:
            return tool_response([call("read", "read_webpage", url=origin + "/page")])
        assert outputs(body)[-1]["status"] == "hook_blocked"
        return answer("denied by configured policy")

    with application(tmp_path, respond, config=config) as (app, store, _):
        assert app.send("read") == "denied by configured policy"
        assert any(e["type"] == "hook_finished" for e in store.load_events(app.session.session_id))
    assert requests == [] and closed == []


def test_parallel_reads_own_distinct_resources_and_keep_structured_output_bound(
    tmp_path, local_web
):
    origin, requests, _, _, closed = local_web
    registry = register_web(
        create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=1024)),
        WebConfig(enabled=True),
    )
    assert registry.capability("read_webpage") is ToolCapability.READ_ONLY
    assert registry.can_parallelize("read_webpage")
    results = []

    def read():
        results.append(registry.execute("read_webpage", json.dumps({"url": origin + "/large"})))

    workers = [threading.Thread(target=read) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert len(requests) == len(results) == len(closed) == 2
    assert closed[0][0] is not closed[1][0]
    for result in results:
        source = json.loads(result)["source"]
        assert len(result) <= 1024 and source["truncated"]
        assert source["final_url"] == origin + "/large" and len(source["sha256"]) == 64


def test_output_too_small_for_source_is_not_a_fake_valid_reference(tmp_path, local_web):
    origin = local_web[0]
    registry = register_web(
        create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=512)),
        WebConfig(enabled=True),
    )
    result = json.loads(
        registry.execute("read_webpage", json.dumps({"url": origin + "/" + "x" * 800}))
    )
    assert not result["ok"] and result["status"] == "output_limit" and "source" not in result


def test_cancel_propagates_and_resource_is_closed_in_own_thread(tmp_path, local_web):
    origin, requests, ready, _, closed = local_web
    cancel = CancellationToken()
    registry = register_web(create_tool_registry(tmp_path), WebConfig(enabled=True))
    interrupted = []

    def read():
        try:
            registry.execute("read_webpage", json.dumps({"url": origin + "/slow"}), cancel=cancel)
        except RequestInterrupted:
            interrupted.append(True)

    worker = threading.Thread(target=read)
    worker.start()
    assert ready.wait(3)
    cancel.cancel()
    worker.join(3)
    assert not worker.is_alive() and interrupted == [True]
    assert len(requests) == len(closed) == 1 and closed[0][1] == worker.ident


def test_reader_construction_failure_closes_already_created_transport(local_web, monkeypatch):
    original = RuntimeError("constructor failure")

    def fail(*args):
        raise original

    monkeypatch.setattr(web_runtime, "WebReader", fail)
    with (
        pytest.raises(RuntimeError) as caught,
        web_runtime.open_reader(FetchLimits(), NetworkPolicy()),
    ):
        pytest.fail("failed construction yielded a service")
    assert caught.value is original and len(local_web[-1]) == 1


def test_cleanup_failure_does_not_mask_initial_error(monkeypatch):
    original = RuntimeError("original")
    closed = []

    class BrokenTransport:
        def __init__(self, *args):
            pass

        def close(self):
            closed.append(True)
            raise OSError("private cleanup details")

    monkeypatch.setattr(web_runtime, "HttpPageTransport", BrokenTransport)
    with (
        pytest.raises(RuntimeError) as caught,
        web_runtime.open_reader(FetchLimits(), NetworkPolicy()),
    ):
        raise original
    assert caught.value is original and closed == [True]
    assert "private" not in " ".join(original.__notes__)


def test_backend_construction_failure_closes_its_runner(monkeypatch):
    closed = []

    class Runner:
        def close(self):
            closed.append(True)

    def fail(**kwargs):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(web_http.asyncio, "Runner", Runner)
    monkeypatch.setattr(web_http.httpx, "AsyncClient", fail)
    with pytest.raises(RuntimeError, match="backend unavailable"):
        web_http.HttpPageTransport(NetworkPolicy(), FetchLimits())
    assert closed == [True]


def test_child_uses_same_registered_capability_with_thread_owned_backend(tmp_path, local_web):
    origin, requests, _, _, closed = local_web
    parent_count = child_count = 0

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        if parent_count == 1:
            result = tool_response([call("spawn", "spawn_agent", name="reader", task="read page")])
        elif parent_count == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            result = answer("child verified page")
            assert (
                outputs(json.loads(request.content))[-1]["agents"][0]["reply"] == "child observed"
            )
        return httpx.Response(200, json=result)

    def child(request):
        nonlocal child_count
        child_count += 1
        body = json.loads(request.content)
        assert "read_webpage" in names(body)
        if child_count == 1:
            result = tool_response([call("read", "read_webpage", url=origin + "/page")])
        else:
            assert outputs(body)[-1]["source"]["text"] == "Report Version one"
            result = answer("child observed")
        return httpx.Response(200, json=result)

    config = configured(tmp_path, tool_discovery=ToolDiscoveryConfig(mode="full"))
    with delegated_application(tmp_path, parent, child, config=config) as (app, _):
        assert app.send("delegate reading") == "child verified page"
    assert len(requests) == len(closed) == 1
    assert closed[0][1] != threading.get_ident() and child_count == 2


def test_recorded_web_read_replays_without_reconnecting(tmp_path, local_web, monkeypatch):
    origin, requests, _, _, closed = local_web

    def respond(body, number):
        result = (
            tool_response([call("read", "read_webpage", url=origin + "/page")])
            if number == 1
            else answer("frozen source")
        )
        return httpx.Response(200, json=result)

    config = configured(tmp_path, tool_discovery=ToolDiscoveryConfig(mode="full"))
    with captured_app(tmp_path, respond, config=config) as (app, recorder, _, source, _):
        assert recorder.run(lambda: app.send("task")) == "frozen source"
    original = source.read_bytes()
    forbid_external_actions(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("offline replay constructed web backend")

    monkeypatch.setattr(web_runtime, "HttpPageTransport", forbidden)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    assert result["reply"] == "frozen source" and source.read_bytes() == original
    assert len(requests) == len(closed) == 1
