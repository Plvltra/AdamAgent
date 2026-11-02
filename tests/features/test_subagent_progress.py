"""B28.2：双向消息经真实父子线程与工具循环落盘；进展不冒充最终结果。"""

import json
import threading

import httpx
import pytest

from adam.application.subagents.mailbox import RunMailbox
from adam.contracts.errors import StorageError, ToolError
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_progress_and_incoming_message_are_persisted_and_collected(tmp_path):
    ready, release = threading.Event(), threading.Event()
    parent_count, child_count = 0, 0
    target = {}

    def child(request):
        nonlocal child_count
        child_count += 1
        body = json.loads(request.content)
        if child_count == 1:
            assert "report_progress" in [tool["function"]["name"] for tool in body["tools"]]
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call(
                            "progress",
                            "report_progress",
                            message_id="p1",
                            text="located evidence, not verified yet",
                        )
                    ]
                ),
            )
        if child_count == 2:
            ready.set()
            assert release.wait(5)
        else:
            assert "check the error case" in body["messages"][-1]["content"]
        return httpx.Response(200, json=answer("child result"))

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        body = json.loads(request.content)
        result = outputs(body)
        if parent_count == 1:
            value = tool_response([call("spawn", "spawn_agent", name="helper", task="investigate")])
        elif parent_count == 2:
            assert ready.wait(3)
            target.update(result[-1])
            value = tool_response(
                [
                    call(
                        "collect",
                        "collect_agent_progress",
                        agent_id=target["agent_id"],
                        run_id=target["run_id"],
                    ),
                    call(
                        "note",
                        "message_agent",
                        agent_id=target["agent_id"],
                        run_id=target["run_id"],
                        message_id="note",
                        text="check the error case",
                    ),
                ]
            )
        elif parent_count == 3:
            assert result[-2]["progress"][0]["text"] == "located evidence, not verified yet"
            assert result[-2]["status"] == "running" and result[-2]["next_cursor"] == 1
            release.set()
            value = tool_response(
                [call("wait", "wait_agents", agent_ids=[target["agent_id"]], timeout_seconds=5)]
            )
        else:
            assert "child result" in json.dumps(result[-1])
            value = answer("done")
        return httpx.Response(200, json=value)

    try:
        with application(tmp_path, parent, child) as (app, store):
            assert app.send("delegate") == "done"
            root = index_for(app).parent / "mailboxes"
            path = root / target["agent_id"] / f"{target['run_id']}.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            assert record["status"] == "completed"
            assert record["incoming"][0]["state"] == "in_context"
            assert record["incoming"][0]["parent_source"]["tool_call_id"] == "note"
            assert record["progress"][0]["state"] == "collected"
            restored = RunMailbox(root, target["agent_id"], target["run_id"], create=False)
            assert restored.queue is None
            assert restored.collect(1)["progress"] == []
            with pytest.raises(ToolError):
                restored.receive("retry", "must not restart", {})
            assert child_count == 3
    finally:
        release.set()


def test_mailbox_failure_dedupe_and_terminal_pending_are_explicit(tmp_path, monkeypatch):
    box = RunMailbox(tmp_path, "1234abcd", "a" * 16)

    def fail():
        raise StorageError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(box, "_save", fail)
        with pytest.raises(StorageError):
            box.receive("one", "text", {})
    assert box.queue.peek() == ()
    assert box.receive("one", "text", {})["sequence"] == 1
    assert box.receive("one", "text", {})["duplicate"]
    assert box.publish("p1", "report")["sequence"] == 1
    assert box.publish("p1", "report")["duplicate"]
    with pytest.raises(ToolError):
        box.publish("p1", "different")
    with pytest.raises(ToolError):
        box.publish("p2", "x" * 2049)
    with pytest.raises(ToolError):
        box.collect(2)
    box.finish("interrupted")
    record = json.loads(box.path.read_text(encoding="utf-8"))
    assert record["incoming"][0]["state"] == "unprocessed"
    restored = RunMailbox(tmp_path, "1234abcd", "a" * 16, create=False)
    assert restored.collect()["progress"][0]["text"] == "report"
    with pytest.raises(ToolError):
        restored.publish("late", "late report")
