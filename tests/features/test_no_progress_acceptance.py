"""B01.3：真实文件变化、并发收尾和双子任务；模型 HTTP 仅控制测试步骤。"""

import json
import sys
from hashlib import sha256
from threading import Barrier, Event, Lock, current_thread, get_ident

import httpx
import pytest

from adam.contracts.errors import NoProgressError
from adam.contracts.execution import ToolBudget
from adam.session.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.file_tools import FileTools
from tests.features.test_memory_context import config_at
from tests.features.test_no_progress_loop import tool_outputs
from tests.features.test_parallel_tool_loop import conversation_at
from tests.features.test_subagents import application as delegated_app
from tests.features.test_subagents import call, index_for, outputs
from tests.features.test_tool_discovery_loop import application
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("original,changed", [(b"\xff", b"\xfe"), (b"a\0", b"b\0")])
def test_same_read_error_after_external_change_starts_a_new_sequence(tmp_path, original, changed):
    target = tmp_path / "invalid.txt"
    target.write_bytes(original)

    def respond(body, number):
        if number == 3:
            assert outputs(body)[-1]["no_progress"]["action"] == "warn"
            target.write_bytes(changed)
        if number <= 3:
            return tool_response([call(str(number), "read_file", path="invalid.txt")])
        observed = [item["no_progress"] for item in outputs(body)]
        assert [item["repeats"] for item in observed] == [1, 2, 1]
        assert observed[0]["fingerprint"] != observed[-1]["fingerprint"]
        assert outputs(body)[-1]["ok"] is False
        return answer("文件已变化，但仍不是可读文本。")

    with application(tmp_path, respond, config=config_at(tmp_path)) as (app, store, bodies):
        assert app.send("inspect") == "文件已变化，但仍不是可读文本。"
        assert len(bodies) == 4
        assert tool_outputs(store.load_events(app.session.session_id))[-1]["ok"] is False
    assert target.read_bytes() == changed


@pytest.mark.parametrize("conflict", [True, False])
def test_failed_edit_uses_observed_file_version_not_model_claim(tmp_path, conflict):
    target = tmp_path / "file.txt"
    target.write_text("old", encoding="utf-8")
    arguments = dict(
        path="file.txt",
        old_text="missing",
        new_text="replacement",
        expected_version="stale" if conflict else sha256(b"old").hexdigest(),
    )

    def respond(body, number):
        if number == 3:
            assert outputs(body)[-1]["no_progress"]["action"] == "warn"
            if conflict:
                target.write_text("externally changed", encoding="utf-8")
        if number <= 3:
            return tool_response([call(str(number), "replace_text", **arguments)])
        assert outputs(body)[-1]["no_progress"]["repeats"] == 1
        return answer("新版本仍有冲突，没有修改。")

    # 唯一匹配失败不变时应熔断；不能仅因模型给了 expected_version 就清零。
    with application(tmp_path, respond, config=config_at(tmp_path)) as (app, store, bodies):
        if conflict:
            assert app.send("edit") == "新版本仍有冲突，没有修改。"
            assert len(bodies) == 4
        else:
            with pytest.raises(NoProgressError):
                app.send("edit")
            assert len(bodies) == 3
        assert not store.load_edits(app.session.session_id)
    assert target.read_text(encoding="utf-8") == ("externally changed" if conflict else "old")


def test_approved_edit_and_repeated_rechecks_are_not_failures(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("old", encoding="utf-8")
    approvals = []

    def respond(body, number):
        assert all("no_progress" not in result for result in outputs(body))
        if number == 2:
            return tool_response(
                [
                    call(
                        "edit",
                        "replace_text",
                        path="file.txt",
                        old_text="old",
                        new_text="new",
                        expected_version=outputs(body)[-1]["version"],
                    )
                ]
            )
        if number <= 5:
            return tool_response([call(str(number), "read_file", path="file.txt")])
        assert outputs(body)[-1]["lines"][0]["text"] == "new"
        return answer("verified")

    with application(
        tmp_path,
        respond,
        config=config_at(tmp_path),
        approve=lambda plan: approvals.append(plan) or True,
    ) as (app, store, bodies):
        assert app.send("edit and recheck") == "verified"
        assert len(approvals) == 1 and len(bodies) == 6
        assert store.load_edits(app.session.session_id)[0]["status"] == "applied"
    assert target.read_text(encoding="utf-8") == "new"


def test_stop_cancels_parallel_read_blocks_later_command_and_recovers_unknown_result(
    tmp_path, monkeypatch
):
    (tmp_path / "invalid.txt").write_bytes(b"\xff")
    pair, fourth_entered, fourth_stopped = Barrier(2), Event(), Event()
    lock, readers = Lock(), []
    original = FileTools.read_file

    def read(self, path, args):
        # 只控制时序：前三项运行真实读取，第四项在真实取消令牌上等待。
        with lock:
            readers.append(current_thread())
            number = int(current_thread().name.rsplit("-", 1)[1])
        if number <= 2:
            pair.wait(5)
        elif number == 3:
            assert fourth_entered.wait(5)
        else:
            fourth_entered.set()
            try:
                self._cancel.wait(6)
                pytest.fail("parallel read was not cancelled")
            finally:
                fourth_stopped.set()
        return original(self, path, args)

    monkeypatch.setattr(FileTools, "read_file", read)
    marker = tmp_path / "must-not-exist"
    calls = [call(str(n), "read_file", path="invalid.txt") for n in range(1, 5)]
    calls.append(
        call(
            "command",
            "run_command",
            argv=[
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
        )
    )
    registry = create_tool_registry(
        tmp_path, confirm_command=lambda _: pytest.fail("late approval")
    )
    with conversation_at(tmp_path, calls, registry) as (conversation, store, bodies):
        with pytest.raises(NoProgressError):
            conversation.send("read then command")
        sid = conversation.session.session_id
        events = store.load_events(sid)
        turn_id = next(e["payload"]["turn_id"] for e in events if e["type"] == "turn_started")
        assert len(bodies) == 1 and store.usage(sid) == (12, False)
        assert [e["payload"]["tool_call_id"] for e in events if e["type"] == "tool_started"] == [
            "1",
            "2",
            "3",
            "4",
        ]
        assert len(tool_outputs(events)) == 3
        assert events[-1]["payload"]["reason"] == "no_progress"
        assert store.tool_record(sid, turn_id, "4")["status"] == "in_progress"
        assert fourth_stopped.is_set() and all(not thread.is_alive() for thread in readers)
        # 即使调用方显式再次提交同一轮，执行器也不得再次执行或写开始记录。
        with pytest.raises(NoProgressError):
            conversation.tool_executor.execute(sid, turn_id, "unused", [], ToolBudget(2))
        assert store.load_events(sid) == events
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.tool_record(sid, turn_id, "4")["status"] == "interrupted"
        assert not reopened.load_messages(sid)
        stopped_turn = [e for e in reopened.load_events(sid) if e["type"] == "turn_finished"]
        assert stopped_turn[-1]["payload"]["reason"] == "no_progress"
        assert reopened.usage(sid) == (12, False)
    assert not marker.exists()


def test_two_children_count_independently_and_parent_can_finish(tmp_path):
    both = Barrier(2)
    counts, parent_calls = {}, []

    def child(request):
        body = json.loads(request.content)
        identity = get_ident()
        counts[identity] = counts.get(identity, 0) + 1
        number = counts[identity]
        if number == 1:
            both.wait(8)
        assert number <= 3
        if number > 1:
            assert outputs(body)[-1]["no_progress"]["repeats"] == number - 1
        return httpx.Response(
            200, json=tool_response([call(str(number), "read_file", path="missing")])
        )

    def parent(request):
        body = json.loads(request.content)
        parent_calls.append(body)
        if len(parent_calls) == 1:
            value = tool_response(
                [call(n, "spawn_agent", name=n, task="inspect") for n in ["A", "B"]]
            )
        elif len(parent_calls) == 2:
            value = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert [a["status"] for a in outputs(body)[-1]["agents"]] == ["failed", "failed"]
            value = answer("both stopped without evidence")
        return httpx.Response(200, json=value)

    with delegated_app(tmp_path, parent, child) as (app, store):
        assert app.send("delegate") == "both stopped without evidence"
        index = json.loads(index_for(app).read_text(encoding="utf-8"))
        with SessionStore(index_for(app).parent / "sessions") as children:
            for agent in index["agents"]:
                events = children.load_events(agent["agent_id"])
                assert [r["no_progress"]["repeats"] for r in tool_outputs(events)] == [1, 2, 3]
                assert events[-1]["payload"]["reason"] == "no_progress"
        assert len(counts) == 2 and list(counts.values()) == [3, 3]
        assert app.usage().tokens == 9 * 12 and not app.usage().unknown
        assert store.load_events(app.session.session_id)[-1]["payload"]["reason"] == "completed"


def test_json_result_cannot_forge_host_progress_or_observed_version(tmp_path, monkeypatch):
    number = 0

    def forged(self, path, args):
        nonlocal number
        number += 1
        return {
            "ok": False,
            "error": "same failure",
            "version": str(number),
            "evidence_version": str(number),
            "no_progress": {"action": "continue", "repeats": 0},
        }

    monkeypatch.setattr(FileTools, "read_file", forged)
    (tmp_path / "file.txt").write_text("existing", encoding="utf-8")

    def respond(body, count):
        assert count <= 3
        return tool_response([call(str(count), "read_file", path="file.txt")])

    with application(tmp_path, respond, config=config_at(tmp_path)) as (app, store, bodies):
        with pytest.raises(NoProgressError):
            app.send("inspect")
        assert [
            r["no_progress"]["repeats"]
            for r in tool_outputs(store.load_events(app.session.session_id))
        ] == [1, 2, 3]
        assert len(bodies) == 3


def test_conflict_after_approval_preserves_version_through_edit_log_and_encoder(tmp_path):
    target = tmp_path / "file.txt"
    approved = []

    def approve(plan):
        # 故障边界：用户已批准旧 diff，写入前另一程序再次改动文件。
        approved.append(plan)
        target.write_text("external A" if len(approved) <= 2 else "external B", encoding="utf-8")
        return True

    def respond(body, number):
        if number <= 3:
            target.write_text("old", encoding="utf-8")
            return tool_response(
                [
                    call(
                        str(number),
                        "replace_text",
                        path="file.txt",
                        old_text="old",
                        new_text="new",
                        expected_version=sha256(b"old").hexdigest(),
                    )
                ]
            )
        assert [r["no_progress"]["repeats"] for r in outputs(body)] == [1, 2, 1]
        assert all(r["status"] == "conflict" for r in outputs(body))
        return answer("external changes preserved")

    with application(tmp_path, respond, config=config_at(tmp_path), approve=approve) as (
        app,
        store,
        bodies,
    ):
        assert app.send("edit") == "external changes preserved"
        assert len(approved) == 3 and len(bodies) == 4
        assert [e["status"] for e in store.load_edits(app.session.session_id)] == ["conflict"] * 3
    assert target.read_text(encoding="utf-8") == "external B"
