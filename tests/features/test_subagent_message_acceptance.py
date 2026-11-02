"""B28.3：双子任务隔离、取消后拒绝补充、独立宿主强杀后的双向消息恢复。"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


def test_two_mailboxes_and_cancel_keep_messages_in_their_run(tmp_path):
    ready = {label: threading.Event() for label in ("A", "B")}
    release = threading.Event()
    counts = {"A": 0, "B": 0}
    step, targets = 0, {}

    def child(request):
        body = json.loads(request.content)
        label = next(
            item["content"] for item in body["messages"] if item.get("content") in ("A", "B")
        )
        counts[label] += 1
        if counts[label] == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call(
                            "report",
                            "report_progress",
                            message_id="same-id",
                            text="progress " + label,
                        )
                    ]
                ),
            )
        if counts[label] == 2:
            ready[label].set()
            assert release.wait(5)
        else:
            assert label == "B" and "note B" in body["messages"][-1]["content"]
            assert "note A" not in json.dumps(body)
        return httpx.Response(200, json=answer("result " + label))

    def parent(request):
        nonlocal step
        step += 1
        result = outputs(json.loads(request.content))
        if step == 1:
            value = tool_response(
                [call(label, "spawn_agent", name=label, task=label) for label in ("A", "B")]
            )
        elif step == 2:
            assert all(item.wait(3) for item in ready.values())
            targets.update(zip(("A", "B"), result[-2:], strict=True))
            value = tool_response(
                [
                    call(
                        "note" + label,
                        "message_agent",
                        agent_id=targets[label]["agent_id"],
                        run_id=targets[label]["run_id"],
                        message_id="same-id",
                        text="note " + label,
                    )
                    for label in ("A", "B")
                ]
            )
        elif step == 3:
            value = tool_response(
                [
                    call("cancel", "interrupt_agent", agent_id=targets["A"]["agent_id"]),
                    call(
                        "late",
                        "message_agent",
                        agent_id=targets["A"]["agent_id"],
                        run_id=targets["A"]["run_id"],
                        message_id="late",
                        text="must reject",
                    ),
                ]
            )
        elif step == 4:
            assert result[-1]["ok"] is False
            release.set()
            value = tool_response([call("wait", "wait_agents", agent_ids=[], timeout_seconds=5)])
        elif step == 5:
            states = {item["name"]: item["status"] for item in result[-1]["agents"]}
            assert states == {"A": "interrupted", "B": "completed"}
            value = tool_response(
                [
                    call(
                        "collect" + label,
                        "collect_agent_progress",
                        agent_id=targets[label]["agent_id"],
                        run_id=targets[label]["run_id"],
                    )
                    for label in ("A", "B")
                ]
            )
        else:
            assert [item["progress"][0]["text"] for item in result[-2:]] == [
                "progress A",
                "progress B",
            ]
            value = answer("done")
        return httpx.Response(200, json=value)

    try:
        with application(tmp_path, parent, child) as (app, store):
            assert app.send("two workers") == "done"
            root = index_for(app).parent / "mailboxes"
            records = {
                label: json.loads(
                    (root / target["agent_id"] / f"{target['run_id']}.json").read_text(
                        encoding="utf-8"
                    )
                )
                for label, target in targets.items()
            }
            assert records["A"]["incoming"][0]["state"] == "unprocessed"
            assert records["B"]["incoming"][0]["state"] == "in_context"
    finally:
        release.set()


@pytest.mark.parametrize("phase", ["pending", "committed"])
def test_killed_host_restores_mailbox_without_reissuing(tmp_path, phase):
    host = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.features.support.subagent_message_worker",
            str(tmp_path),
            phase,
        ],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        ready = tmp_path / "ready.json"
        deadline = time.monotonic() + 8
        while not ready.exists() and host.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "独立宿主未到达邮箱持久边界"
        identity = json.loads(ready.read_text())
        host.kill()
        host.wait(timeout=5)
    finally:
        if host.poll() is None:
            host.kill()
        host.communicate(timeout=5)
    before_requests = {
        role: (tmp_path / (role + "-requests")).read_bytes() for role in ("parent", "child")
    }

    def no_request(request):
        pytest.fail("恢复不能发起模型请求")

    with application(tmp_path, no_request, no_request) as (app, store):
        app.resume(identity["session_id"])
        app.usage()  # 原共享账本选择触发子日志、索引及邮箱一起恢复。
        path = (
            index_for(app).parent
            / "mailboxes"
            / identity["agent_id"]
            / f"{identity['run_id']}.json"
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == ("completed" if phase == "committed" else "interrupted")
        assert record["incoming"][0]["state"] == (
            "in_context" if phase == "committed" else "unprocessed"
        )
        assert record["progress"][0]["text"] == "evidence found"
        assert record["progress"][0]["state"] == "reported"
        before = path.read_bytes()
        app.usage()
        assert path.read_bytes() == before
        assert all(
            (tmp_path / (role + "-requests")).read_bytes() == value
            for role, value in before_requests.items()
        )
