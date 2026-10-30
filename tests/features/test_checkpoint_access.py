"""检查点副本保持内部用途；实际主/子工具均不能借此扩大读取范围。"""

import json

import httpx

from tests.features.test_subagents import application, call, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_parent_and_child_cannot_read_saved_original_from_checkpoint_directory(tmp_path):
    target = tmp_path / "source.txt"
    target.write_bytes(b"old")
    parent_requests, child_requests, approvals = [], [], []
    checkpoint_path = None

    def child(request):
        body = json.loads(request.content)
        child_requests.append(body)
        if len(child_requests) == 1:
            return httpx.Response(
                200, json=tool_response([call("probe", "read_file", path=str(checkpoint_path))])
            )
        result = outputs(body)[-1]
        assert result["ok"] is False and "保留目录" in result["error"]
        return httpx.Response(200, json=answer("checkpoint denied"))

    def parent(request):
        nonlocal checkpoint_path
        body = json.loads(request.content)
        parent_requests.append(body)
        phase = len(parent_requests)
        if phase == 1:
            batch = [call("read", "read_file", path="source.txt")]
        elif phase == 2:
            batch = [
                call(
                    "edit",
                    "replace_text",
                    path="source.txt",
                    old_text="old",
                    new_text="new",
                    expected_version=outputs(body)[-1]["version"],
                )
            ]
        elif phase == 3:
            assert outputs(body)[-1]["status"] == "applied"
            checkpoint_path = next((tmp_path / "sessions" / "_checkpoints").glob("*/*.json"))
            batch = [
                call("parent-probe", "read_file", path=str(checkpoint_path)),
                call("child", "spawn_agent", name="reader", task="Read " + str(checkpoint_path)),
            ]
        elif phase == 4:
            values = outputs(body)
            assert values[-2]["ok"] is False and "保留目录" in values[-2]["error"]
            batch = [call("wait", "wait_agents", agent_ids=[values[-1]["agent_id"]])]
        else:
            result = outputs(body)[-1]["agents"][0]
            assert result["status"] == "completed" and result["reply"] == "checkpoint denied"
            return httpx.Response(200, json=answer("both denied"))
        return httpx.Response(200, json=tool_response(batch))

    with application(
        tmp_path, parent, child, confirm_edit=lambda plan: approvals.append(plan) or True
    ) as (app, store):
        assert app.send("edit and inspect boundaries") == "both denied"
        assert len(approvals) == 1 and target.read_bytes() == b"new"
        edit = store.load_edits(app.session.session_id)[0]
        assert (
            store.load_edit_checkpoint(
                app.session.session_id, edit["edit_id"], max_bytes=100
            ).original
            == b"old"
        )
    assert len(parent_requests) == 5 and len(child_requests) == 2
