"""T11.3：两个实际子任务的修改提案、冲突和主会话隔离。"""

import json
import threading

import httpx
import pytest

from adam.contracts.errors import ApprovalDeniedError
from adam.session.storage import SessionStore
from tests.features.test_proposal_application import proposal_workflow
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize(
    "case", ["distinct", "conflict", "deny", "second_conflict", "child_failed"]
)
def test_two_real_helpers_propose_then_parent_applies_without_overwriting(tmp_path, case):
    first, second = tmp_path / "first.py", tmp_path / "second.py"
    first.write_bytes(b"value = 1\n")
    second.write_bytes(b"value = 1\n")
    targets = {"A": first, "B": first if case == "conflict" else second}
    both_read = threading.Barrier(2)
    approvals, parent_requests, child_threads = [], [], set()
    dispatched, selected, final_outputs = [], [], []

    def child(request):
        body = json.loads(request.content)
        child_threads.add(threading.get_ident())
        task = next(
            message["content"]
            for message in reversed(body["messages"])
            if message["role"] == "user"
        )
        observed = outputs(body)
        if not observed:
            result = tool_response([call("read", "read_file", path=str(targets[task]))])
        elif len(observed) == 1:
            # 两个真实读取都已落盘，再允许任一子任务提交；父任务还没有修改文件。
            assert observed[-1]["ok"] and "version" in observed[-1]
            both_read.wait(timeout=10)
            result = tool_response(
                [
                    call(
                        "submit",
                        "submit_proposal",
                        read_call_id="read",
                        old_text="value = 1",
                        new_text="value = 2" if task == "A" else "value = 3",
                        reason="correction " + task,
                    )
                ]
            )
        else:
            assert observed[-1]["status"] == "submitted"
            result = answer("candidate ready " + task)
            if case == "child_failed" and task == "B":
                # 用量已知但子轮协议失败，候选只保留作诊断，不能拖累另一份成功候选。
                result["choices"][0]["message"]["role"] = "user"
        return httpx.Response(200, json=result)

    def parent(request):
        body = json.loads(request.content)
        parent_requests.append(body)
        phase = len(parent_requests)
        if phase == 1:
            result = tool_response(
                [
                    call("spawnA", "spawn_agent", name="A", task="A"),
                    call("spawnB", "spawn_agent", name="B", task="B"),
                ]
            )
        elif phase == 2:
            dispatched.extend(outputs(body)[-2:])
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        elif phase == 3:
            assert [item["status"] for item in outputs(body)[-1]["agents"]] == [
                "completed",
                "failed" if case == "child_failed" else "completed",
            ]
            assert first.read_bytes() == second.read_bytes() == b"value = 1\n"
            result = tool_response(
                [
                    call(
                        "collect" + str(index),
                        "collect_proposals",
                        agent_id=item["agent_id"],
                        run_id=item["run_id"],
                    )
                    for index, item in enumerate(dispatched)
                ]
            )
        elif phase == 4:
            pages = outputs(body)[-2:]
            if case == "child_failed":
                assert pages[1]["ok"] is False
            selected.extend(item for item in pages if item["ok"])
            assert len({item["proposal_id"] for item in selected}) == (
                1 if case == "child_failed" else 2
            )
            result = tool_response(
                [
                    call(
                        "apply" + str(index),
                        "apply_proposal",
                        **{key: item[key] for key in ("agent_id", "run_id", "proposal_id", "path")},
                    )
                    for index, item in enumerate(selected)
                ]
            )
        else:
            final_outputs.extend(outputs(body)[-len(selected) :])
            result = answer("reviewed actual application outcomes")
        return httpx.Response(200, json=result)

    def confirm(plan):
        approvals.append((threading.get_ident(), plan))
        if case == "second_conflict" and len(approvals) == 2:
            second.write_bytes(b"external = 9\n")
        return case != "deny"

    with application(tmp_path, parent, child, confirm_edit=confirm) as (app, store):
        if case == "deny":
            with pytest.raises(ApprovalDeniedError):
                app.send("delegate two corrections")
        else:
            assert app.send("delegate two corrections") == "reviewed actual application outcomes"
        sid = app.session.session_id
        edits = store.load_edits(sid)
        assert len(child_threads) == 2 and threading.get_ident() not in child_threads
        assert all(thread == threading.get_ident() for thread, _ in approvals)
        assert app.usage().tokens == (10 if case == "deny" else 11) * 12
        assert not app.usage().unknown
        expected = {
            "distinct": ([b"value = 2\n", b"value = 3\n"], ["applied", "applied"], 2),
            "conflict": ([b"value = 2\n", b"value = 1\n"], ["applied"], 1),
            "deny": ([b"value = 1\n", b"value = 1\n"], ["denied"], 1),
            "second_conflict": ([b"value = 2\n", b"external = 9\n"], ["applied", "conflict"], 2),
            "child_failed": ([b"value = 2\n", b"value = 1\n"], ["applied"], 1),
        }[case]
        assert [first.read_bytes(), second.read_bytes()] == expected[0]
        assert [item["status"] for item in edits] == expected[1]
        assert len(approvals) == expected[2]
        if case != "deny":
            assert final_outputs[0]["status"] == "applied"
            if case != "child_failed":
                assert final_outputs[1]["ok"] is (case == "distinct")
        if case == "conflict":
            assert "变化" in final_outputs[1]["error"]
            assert [action.status for action in app.execution_report().actions] == [
                "applied",
                "not_started",
            ]
        index = json.loads(index_for(app).read_text(encoding="utf-8"))
        assert len(index["agents"]) == 2
        proposals = []
        with SessionStore(store.root / "_subagents" / sid / "sessions") as children:
            for agent in index["agents"]:
                assert agent["runs"][0]["status"] == (
                    "failed" if case == "child_failed" and agent["name"] == "B" else "completed"
                )
                assert children.load_edits(agent["agent_id"]) == []
                saved = children.load_proposals(agent["agent_id"])
                assert len(saved) == 1
                proposals.extend(saved)
        assert {item.owner.child_session_id for item in proposals} == {
            item["agent_id"] for item in dispatched
        }
        assert len({item.read.version for item in proposals}) == 1
        for edit in edits:
            assert edit["proposal"]["proposal_id"] in {item.proposal_id for item in proposals}
            if edit["status"] == "applied":
                checkpoint = store.load_edit_checkpoint(sid, edit["edit_id"], max_bytes=1024)
                assert checkpoint.original == b"value = 1\n"


def test_another_main_session_cannot_apply_saved_child_proposal(tmp_path):
    with proposal_workflow(tmp_path) as env:
        saved = {}

        def actions(body):
            phase = len(env.parent_requests)
            if phase == 4:
                page = outputs(body)[-1]
                saved.update(
                    {key: page[key] for key in ("agent_id", "run_id", "proposal_id", "path")}
                )
                return answer("candidate only")
            if phase == 5:
                return tool_response([call("foreign", "apply_proposal", **saved)])
            assert outputs(body)[-1]["ok"] is False
            return answer("cross-session proposal refused")

        env.followup = actions
        assert env.app.send("save without applying") == "candidate only"
        original_sid = env.app.session.session_id
        env.app.new("separate main session")
        assert env.app.send("apply the other session candidate") == "cross-session proposal refused"
        assert env.app.session.session_id != original_sid
        assert env.target.read_bytes() == b"value = 1\n" and env.approvals == []
        assert env.store.load_edits(original_sid) == []
        assert env.store.load_edits(env.app.session.session_id) == []
