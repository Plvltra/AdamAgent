"""E1.4：继承固定快照与故障降级，经过真实委派线程、HTTP 适配和会话日志。"""

import json
from pathlib import Path

import httpx
import pytest

from adam.bootstrap import build_application
from adam.cli.main import handle_command
from adam.contracts.errors import MemoryStoreError, StorageError
from adam.contracts.memory import MemoryCommand
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import completion, config_at, memory_records
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_child_inherits_parent_snapshot_then_fresh_selection_on_followup(tmp_path):
    config = config_at(tmp_path)
    parent_bodies, child_bodies = [], []
    memory_file = config.memories_dir / "memories.json"

    def parent(request):
        body = json.loads(request.content)
        parent_bodies.append(body)
        step = len(parent_bodies)
        if step == 1:
            # 主轮次已选择 v1，但子线程尚未创建；库的更新不能改变本轮快照。
            app.manage_memory(
                MemoryCommand("update", "Python 使用新约定", item.memory_id, revision=1)
            )
            data = tool_response([call("spawn", "spawn_agent", name="helper", task="explain Rust")])
        elif step == 4:
            data = tool_response(
                [call("follow", "send_message", agent_id=child_id, task="continue")]
            )
        elif step in {2, 5}:
            data = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            data = answer("parent result")
        return httpx.Response(200, json=data)

    def child(request):
        body = json.loads(request.content)
        child_bodies.append(body)
        assert not any("memory" in tool["function"]["name"] for tool in body["tools"])
        if len(child_bodies) == 1:
            data = tool_response(
                [
                    call("read", "read_file", path=str(memory_file)),
                    call("search", "search_text", path=str(config.memories_dir), query="Rust"),
                    call("scan", "search_text", path=str(tmp_path), query="HIDDEN_SECRET"),
                    call(
                        "edit",
                        "replace_text",
                        path=str(memory_file),
                        old_text="Rust",
                        new_text="changed",
                        expected_version="v",
                    ),
                ]
            )
        elif len(child_bodies) == 2:
            results = outputs(body)
            assert results[0]["ok"] is False and results[1]["ok"] is False
            # 搜索自己的工具日志可能命中查询文本，但不能读到库中的未选正文。
            assert all(not hit["path"].startswith("memory/") for hit in results[2]["results"])
            assert "Rust HIDDEN_SECRET" not in json.dumps(results[2], ensure_ascii=False)
            assert results[3]["status"] == "approval_required"
            data = answer("child result")
        else:
            data = answer("child followup")
        return httpx.Response(200, json=data)

    with application(tmp_path, parent, child, config=config) as (app, store):
        app.new("source")
        item = app.manage_memory(MemoryCommand("add", "Python 使用旧约定"))[0]
        hidden = app.manage_memory(MemoryCommand("add", "Rust HIDDEN_SECRET"))[0]
        app.send("Python 约定")
        child_id = json.loads(index_for(app).read_text(encoding="utf-8"))["agents"][0]["agent_id"]
        assert {r["revision"] for r in memory_records(parent_bodies[0])} == {1}
        assert memory_records(child_bodies[0]) == memory_records(parent_bodies[0])
        assert memory_records(child_bodies[1]) == memory_records(child_bodies[0])
        assert hidden.memory_id not in {r["memory_id"] for r in memory_records(child_bodies[0])}
        app.send("Python 新约定")
        assert memory_records(child_bodies[-1]) == memory_records(parent_bodies[3])
        assert {r["revision"] for r in memory_records(child_bodies[-1])} == {2}
        assert {r.content for r in app.manage_memory(MemoryCommand("list"))} == {
            "Python 使用新约定",
            "Rust HIDDEN_SECRET",
        }
        parent_id = app.session.session_id
        child_root = store.root / "_subagents" / parent_id / "sessions"
        with SessionStore(child_root) as children:
            children.select_session(child_id)
            records = [
                event["payload"]["context"]["memory"]
                for event in children.load_events(child_id)
                if event["type"] == "request_started"
            ]
            assert [r["records"][0]["revision"] for r in records] == [1, 1, 2]
            assert records[0]["query"] == "Python 约定"


@pytest.mark.parametrize("fault", ["corrupt", "permission"])
def test_unreadable_memory_warns_once_and_chat_still_commits(tmp_path, monkeypatch, fault):
    config = config_at(tmp_path)
    payloads, notices = [], []
    (tmp_path / "note.txt").write_text("evidence", encoding="utf-8")

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion(tool=len(payloads) == 1))

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), tmp_path, on_notice=notices.append
        )
        app.manage_memory(MemoryCommand("add", "Python 约定"))
        path = config.memories_dir / "memories.json"
        if fault == "corrupt":
            path.write_bytes(b"{unfinished")
        original = path.read_bytes()
        real_open = Path.open

        def denied(target, *args, **kwargs):
            # 只模拟记忆文件读取权限故障，会话 JSONL 仍使用真实文件操作。
            if target == path and args and args[0] == "rb":
                raise PermissionError("simulated memory access denied")
            return real_open(target, *args, **kwargs)

        with monkeypatch.context() as patch:
            if fault == "permission":
                patch.setattr(Path, "open", denied)
            assert app.send("Python 约定") == "answer"
            assert sum("记忆读取失败" in text for text in notices) == 1
            assert all(memory_records(body) == [] for body in payloads)
            assert app.preview_context("Python").memory.status == "unavailable"
            shown = []
            with pytest.raises(MemoryStoreError):
                handle_command("/memory add -- Python new", app, shown.append)
            assert not any("已保存" in text for text in shown)
        assert path.read_bytes() == original
        assert len(store.load_turns(app.session.session_id)) == 1
        snapshots = [
            event["payload"]["context"]["memory"]
            for event in store.load_events(app.session.session_id)
            if event["type"] == "request_started"
        ]
        assert len(snapshots) == 2
        assert all(s["status"] == "unavailable" and s["warning"] for s in snapshots)


def test_memory_degradation_does_not_hide_session_log_failure(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    config.memories_dir.mkdir()
    (config.memories_dir / "memories.json").write_text("broken", encoding="utf-8")
    requested = []

    def handler(request):
        requested.append(request)
        return httpx.Response(200, json=completion())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        SessionStore(config.sessions_dir) as store,
    ):
        app = build_application(config, store, LLMClient(config.llm, http), tmp_path)
        app.new("before fault")

        def fail(*args, **kwargs):
            raise StorageError("session history unavailable")

        monkeypatch.setattr(store, "load_turns", fail)
        with pytest.raises(StorageError, match="session history unavailable"):
            app.send("Python")
        assert requested == []
