"""B14.2.2：生产组装中的代码工具；隔离传输替身与真实 OS 验收分别记录。"""

import json
import sys
from dataclasses import replace

import httpx
import pytest

import adam.isolation.rpc as rpc_module
from adam.config.loader import load_config
from adam.config.settings import OrchestrationConfig, ToolDiscoveryConfig
from adam.contracts.errors import BudgetError, ConfigError, IsolationError
from adam.contracts.orchestration import OrchestrationResult
from adam.isolation.backend import IsolationCapability, WslBubblewrap
from adam.isolation.rpc import IsolatedOrchestrator
from adam.recording.runtime import replay_turn
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_task_planning import step
from tests.features.test_tool_discovery_loop import names
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM


def enabled_config(root, **changes):
    return replace(config_at(root), orchestration=OrchestrationConfig(enabled=True), **changes)


def install_transport(monkeypatch, operation):
    """仅替换隔离传输；回调仍使用生产执行器，不 eval 模型代码，也不声称验证 OS。"""
    calls = []

    def run(self, code, invoke, *, cancel=None):
        calls.append((code, cancel))
        return operation(invoke, cancel)

    monkeypatch.setattr(IsolatedOrchestrator, "run", run)
    return calls


def success(result, calls=0):
    return OrchestrationResult(result, True, 0, "exited", calls, "")


def test_on_demand_discovery_runs_original_file_tools_then_replays_without_backend(
    tmp_path, monkeypatch
):
    (tmp_path / "a.txt").write_text("worker-only text", encoding="utf-8")

    def transport(invoke, cancel):
        info = invoke("file_info", {"path": "a.txt"})
        content = invoke("read_file", {"path": "a.txt"})
        return success({"size": info["size"], "count": len(content["lines"])}, 2)

    transport_calls = install_transport(monkeypatch, transport)
    config = enabled_config(tmp_path, tool_discovery=ToolDiscoveryConfig())

    def respond(body, number):
        if number == 1:
            assert names(body) == ["list_tools", "discover_tools"]
            value = tool_response(
                [
                    call(
                        "discover",
                        "discover_tools",
                        names=["execute_code", "file_info", "read_file"],
                    )
                ]
            )
        elif number == 2:
            assert {"execute_code", "file_info", "read_file"} <= set(names(body))
            value = tool_response(
                [call("code", "execute_code", code="result = 'transport fixture'")]
            )
        else:
            value = outputs(body)[-1]
            assert value["ok"] and value["has_result"] and value["tool_calls"] == 2
            assert value["result"] == {"size": 16, "count": 1}
            assert "worker-only text" not in json.dumps(body["messages"])
            value = answer("summary")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, bodies):
        assert recorder.run(lambda: app.send("task")) == "summary"
        events = store.load_events(app.session.session_id)
        started = [e["payload"] for e in events if e["type"] == "tool_started"]
        assert [e["name"] for e in started] == [
            "discover_tools",
            "execute_code",
            "file_info",
            "read_file",
        ]
        assert all(e["parent_tool_call_id"] == "code" for e in started[2:])
        assert len(transport_calls) == 1
        actual = [
            e["payload"]["context"]["runtime_state"]
            for e in events
            if e["type"] == "request_started"
        ]
        assert actual[-1]["tools"]["used"] == 4
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        IsolatedOrchestrator, "run", lambda *args, **kwargs: pytest.fail("replay started worker")
    )
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"


def test_disabled_entry_neither_exposes_nor_runs_backend(tmp_path, monkeypatch):
    install_transport(monkeypatch, lambda *_: pytest.fail("disabled backend invoked"))

    def respond(body, number):
        assert "execute_code" not in names(body)
        return httpx.Response(200, json=answer("ordinary chat"))

    with captured_app(tmp_path, respond) as (app, recorder, _, _, _):
        assert recorder.run(lambda: app.send("task")) == "ordinary chat"


@pytest.mark.parametrize(
    "code", ["", "x" * 65537, "\ud800"], ids=["empty", "oversize", "invalid_utf8"]
)
def test_invalid_code_is_rejected_before_isolation_transport(tmp_path, monkeypatch, code):
    install_transport(monkeypatch, lambda *_: pytest.fail("invalid code reached backend"))

    def respond(body, number):
        if number == 1:
            value = tool_response([call("bad", "execute_code", code=code)])
        else:
            assert not outputs(body)[-1]["ok"]
            value = answer("rejected")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=enabled_config(tmp_path)) as (
        app,
        recorder,
        store,
        _,
        _,
    ):
        assert recorder.run(lambda: app.send("task")) == "rejected"
        assert not any(
            "parent_tool_call_id" in e["payload"] for e in store.load_events(app.session.session_id)
        )


@pytest.mark.parametrize("failure", ["unavailable", "worker_exit", "limit"])
def test_backend_and_budget_failures_keep_distinct_facts_and_do_not_retry(
    tmp_path, monkeypatch, failure
):
    (tmp_path / "a.txt").write_text("evidence", encoding="utf-8")

    def operation(invoke, cancel):
        if failure == "unavailable":
            raise IsolationError("隔离不可用；未执行。")
        invoke("read_file", {"path": "a.txt"})
        if failure == "limit":
            invoke("file_info", {"path": "a.txt"})
        return OrchestrationResult(None, False, 7, "exited", 1, "script failed")

    calls = install_transport(monkeypatch, operation)
    config = enabled_config(tmp_path)
    config = replace(config, agent=replace(config.agent, max_tool_calls=2))

    def respond(body, number):
        if number == 1:
            value = tool_response([call("code", "execute_code", code="result = None")])
        else:
            assert failure == "worker_exit"
            result = outputs(body)[-1]
            assert not result["ok"] and not result["has_result"] and result["exit_code"] == 7
            value = answer("script failed")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, bodies):
        if failure == "worker_exit":
            assert recorder.run(lambda: app.send("task")) == "script failed"
        else:
            with pytest.raises(IsolationError if failure == "unavailable" else BudgetError):
                recorder.run(lambda: app.send("task"))
            assert len(bodies) == 1
            assert store.load_messages(app.session.session_id)[-1].content == "old answer"
        assert len(calls) == 1
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        IsolatedOrchestrator, "run", lambda *args, **kwargs: pytest.fail("replay ran backend")
    )
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"


def test_child_code_tool_keeps_delegated_process_restriction(tmp_path, monkeypatch):
    def operation(invoke, cancel):
        assert cancel is not None
        return success(
            invoke("run_command", {"argv": [sys.executable, "-c", "raise SystemExit(91)"]}), 1
        )

    install_transport(monkeypatch, operation)
    children = []

    def child(request):
        body = json.loads(request.content)
        children.append(body)
        assert "execute_code" in names(body)
        if not outputs(body):
            value = tool_response([call("code", "execute_code", code="result = 'child'")])
        else:
            result = outputs(body)[-1]["result"]
            assert result["status"] == "approval_required" and not result["ok"]
            value = answer("needs parent approval")
        return httpx.Response(200, json=value)

    def parent(body, count):
        if count == 1:
            value = tool_response([call("spawn", "spawn_agent", name="helper", task="inspect")])
        elif count == 2:
            assert outputs(body)[-1]["ok"]
            value = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            assert outputs(body)[-1]["agents"][0]["reply"] == "needs parent approval"
            value = answer("child checked")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, parent, config=enabled_config(tmp_path), child_handler=child) as (
        app,
        recorder,
        _,
        _,
        _,
    ):
        assert recorder.run(lambda: app.send("task")) == "child checked"
        assert len(children) == 2


@pytest.mark.parametrize(
    "section",
    [
        '[orchestration]\nenabled="yes"',
        '[orchestration]\ndistribution="--unsafe"',
        '[orchestration]\ndistribution="Ubuntu /bin/sh"',
        "[orchestration]\nfallback=true",
    ],
)
def test_config_rejects_ambiguous_backend_or_host_fallback(tmp_path, section):
    path = tmp_path / "config.toml"
    path.write_text(section, encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    with pytest.raises(ConfigError, match="orchestration"):
        load_config(path)


def test_config_selects_distribution_and_defaults_disabled(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    assert not load_config(path).orchestration.enabled
    path.write_text('[orchestration]\nenabled=true\ndistribution="Ubuntu-24.04"', encoding="utf-8")
    assert load_config(path).orchestration == OrchestrationConfig(True, "Ubuntu-24.04")


def test_launcher_disappearing_after_probe_is_a_controlled_failure(monkeypatch):
    """模拟探测成功后启动器被删除；不得将启动竞争变成宿主回退或原始系统错误。"""
    backend = WslBubblewrap()
    monkeypatch.setattr(backend, "probe", lambda: IsolationCapability(True, "verified"))

    async def missing(*args, **kwargs):
        raise FileNotFoundError("injected private launcher path")

    monkeypatch.setattr(rpc_module.asyncio, "create_subprocess_exec", missing)
    with pytest.raises(IsolationError, match="启动失败") as error:
        IsolatedOrchestrator(backend).run("result = 1", lambda *_: pytest.fail("callback ran"))
    assert error.value.code.value == "isolation_error"
    assert "private launcher" not in str(error.value)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_plan_completion_checks_real_nested_command_not_script_success(
    tmp_path, monkeypatch, exit_code
):
    """隔离传输替身调用一次真实命令；正常聚合不能把退出 7 改写成完成证据。"""

    def operation(invoke, cancel):
        invoke("run_command", {"argv": [sys.executable, "-c", f"raise SystemExit({exit_code})"]})
        return success({"summary": "script completed"}, 1)

    install_transport(monkeypatch, operation)
    nested_id = None

    def respond(body, number):
        nonlocal nested_id
        if number == 1:
            value = tool_response(
                [
                    call("plan", "update_plan", steps=[step("in_progress")]),
                    call("code", "execute_code", code="result = 'command summary'"),
                ]
            )
        elif number == 2:
            result = outputs(body)[-1]
            assert result["ok"] and result["result"]["summary"] == "script completed"
            nested_id = result["nested_calls"][0]["tool_call_id"]
            value = tool_response(
                [call("complete", "update_plan", steps=[step("completed", ["code"])])]
            )
        elif number == 3 and exit_code:
            assert not outputs(body)[-1]["ok"]
            value = tool_response(
                [
                    call(
                        "failed",
                        "update_plan",
                        steps=[step("failed", [nested_id], "command exited nonzero")],
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["ok"]
            value = answer("recorded outcome")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=enabled_config(tmp_path)) as (
        app,
        recorder,
        store,
        _,
        _,
    ):
        assert recorder.run(lambda: app.send("task")) == "recorded outcome"
        operations = store.load_executions(app.session.session_id)
        assert len(operations) == 1 and operations[0]["exit_code"] == exit_code
        assert operations[0]["tool_call_id"] == nested_id
