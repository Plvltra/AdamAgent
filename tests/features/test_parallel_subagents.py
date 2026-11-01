"""T6.3：两个子 Agent 各自并发读取，验证独立取消、共享用量与会话日志归属。"""

import json
from dataclasses import replace
from threading import Barrier, Event, Lock, current_thread, get_ident

import httpx
import pytest

from adam.session.storage import SessionStore
from adam.tool_calls.file_tools import FileTools
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_hooks import hooks_at
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("cancel_one", [False, True])
def test_parallel_children_keep_sources_and_cancellation_isolated(
    tmp_path, monkeypatch, cancel_one
):
    """四个实际读取在屏障处相遇；取消 A 时 B 和主会话仍能完成，无遗留工作线程。"""
    for label in ("A", "B"):
        for number in (1, 2):
            (tmp_path / f"{label}{number}.txt").write_text(f"{label}{number}")
    all_reading = Barrier(4)
    ready = Event()
    mutex = Lock()
    workers, finished, journal_threads = [], [], {}
    child_requests, parent_requests, coordinators = {"A": [], "B": []}, [], {}
    original_read = FileTools.read_file

    def read(self, path, args):
        result = original_read(self, path, args)
        with mutex:
            workers.append(current_thread())
        try:
            all_reading.wait(timeout=12)
            ready.set()
            if cancel_one and path.stem.startswith("A"):
                self._cancel.wait(12)
                pytest.fail("child A did not receive cancellation")
            return result
        finally:
            with mutex:
                finished.append(path.stem)

    monkeypatch.setattr(FileTools, "read_file", read)
    for method in ("append_tool_event", "append_hook_event"):
        original = getattr(SessionStore, method)

        def record(self, session_id, kind, data, original=original):
            with mutex:
                journal_threads.setdefault(session_id, set()).add(get_ident())
            return original(self, session_id, kind, data)

        monkeypatch.setattr(SessionStore, method, record)

    def child(request):
        body = json.loads(request.content)
        label = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        with mutex:
            child_requests[label].append(body)
            coordinators[label] = get_ident()
        assert "当前执行模式：plan。" in body["messages"][0]["content"]
        if len(child_requests[label]) == 1:
            calls = [call(f"{label}{i}", "read_file", path=f"{label}{i}.txt") for i in (1, 2)]
            return httpx.Response(200, json=tool_response(calls))
        assert not (cancel_one and label == "A")
        messages = [m for m in body["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in messages] == [label + "1", label + "2"]
        assert [r["lines"][0]["text"] for r in outputs(body)] == [label + "1", label + "2"]
        return httpx.Response(200, json=answer("verified " + label))

    def parent(request):
        body = json.loads(request.content)
        parent_requests.append(body)
        step = len(parent_requests)
        if step == 1:
            calls = [
                call("spawn-" + label, "spawn_agent", name=label, task=label)
                for label in ("A", "B")
            ]
        elif step == 2 and cancel_one:
            assert ready.wait(12)
            agent_id = outputs(body)[0]["agent_id"]
            calls = [call("cancel-A", "interrupt_agent", agent_id=agent_id)]
        elif step == (3 if cancel_one else 2):
            calls = [call("wait", "wait_agents", agent_ids=[])]
        else:
            collected = {agent["name"]: agent for agent in outputs(body)[-1]["agents"]}
            assert collected["B"]["reply"] == "verified B"
            assert collected["A"]["status"] == ("interrupted" if cancel_one else "completed")
            if not cancel_one:
                assert collected["A"]["reply"] == "verified A"
            return httpx.Response(200, json=answer("parent completed"))
        return httpx.Response(200, json=tool_response(calls))

    config = replace(config_at(tmp_path), hooks=hooks_at(tmp_path))
    with application(tmp_path, parent, child, config=config) as (app, store):
        app.select_mode("plan")
        assert app.send("delegate isolated reads") == "parent completed"
        sid = app.session.session_id
        index = json.loads(index_for(app).read_text(encoding="utf-8"))
        assert len(index["agents"]) == 2 and ready.is_set()
        assert journal_threads[sid] == {get_ident()}
        assert len(set(coordinators.values())) == 2
        assert sorted(finished) == ["A1", "A2", "B1", "B2"]
        assert len(workers) == 4 and all(not worker.is_alive() for worker in workers)
        assert not {worker.ident for worker in workers} & set(coordinators.values())
        assert app.usage().tokens == 84 and not app.usage().unknown
        assert len(parent_requests) == (4 if cancel_one else 3)
        assert len(child_requests["A"]) == (1 if cancel_one else 2)
        assert len(child_requests["B"]) == 2
        parent_turn = next(
            e["payload"]["turn_id"] for e in store.load_events(sid) if e["type"] == "turn_started"
        )
        child_root = index_for(app).parent / "sessions"
        with SessionStore(child_root) as children:
            for agent in index["agents"]:
                label, child_id = agent["name"], agent["agent_id"]
                interrupted = cancel_one and label == "A"
                assert journal_threads[child_id] == {coordinators[label]}
                events = children.load_events(child_id)
                starts = [e["payload"] for e in events if e["type"] == "tool_started"]
                ends = [e["payload"] for e in events if e["type"] == "tool_finished"]
                assert [e["tool_call_id"] for e in starts] == [label + "1", label + "2"]
                assert len(ends) == (0 if interrupted else 2)
                assert len({e["request_id"] for e in starts}) == 1
                assert {e["turn_id"] for e in starts} == {agent["runs"][0]["child_turn_id"]}
                assert agent["runs"][0]["parent_turn_id"] == parent_turn
                hooks = children.load_hooks(child_id)
                assert len(hooks) == (2 if interrupted else 4)
                assert {h["execution_mode"] for h in hooks} == {"plan"}
                assert events[-1]["payload"]["status"] == (
                    "interrupted" if interrupted else "succeeded"
                )
                history = children.load_messages(child_id)
                assert bool(history) is not interrupted
