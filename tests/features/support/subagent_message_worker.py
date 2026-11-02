"""B28.3：独立父子宿主在邮箱接受或成功收尾边界等待父测试强杀。"""

import json
import sys
import threading
import time
from pathlib import Path

import httpx

from adam.application.subagents.mailbox import RunMailbox
from tests.features.test_subagents import application, call, outputs
from tests.features.test_tool_loop import answer, tool_response


def run(root, phase):
    ready, release = threading.Event(), threading.Event()
    steps = {"parent": 0, "child": 0}

    def count(role):
        steps[role] += 1
        with (root / (role + "-requests")).open("a", encoding="utf-8") as stream:
            stream.write("request\n")
        return steps[role]

    def pause():
        path = next((root / "sessions" / "_subagents").glob("*/mailboxes/*/*.json"))
        value = {
            "session_id": path.parents[2].name,
            "agent_id": path.parent.name,
            "run_id": path.stem,
        }
        (root / "ready.tmp").write_text(json.dumps(value), encoding="utf-8")
        (root / "ready.tmp").replace(root / "ready.json")
        while True:
            time.sleep(0.1)

    original = RunMailbox.finish

    def finish(self, status):
        if phase == "committed" and status == "completed":
            pause()
        return original(self, status)

    RunMailbox.finish = finish

    def child(request):
        number = count("child")
        if number == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [call("report", "report_progress", message_id="p1", text="evidence found")]
                ),
            )
        if number == 2:
            ready.set()
            assert release.wait(30)
        return httpx.Response(200, json=answer("child finished"))

    def parent(request):
        number = count("parent")
        body = json.loads(request.content)
        if number == 1:
            value = tool_response([call("spawn", "spawn_agent", name="helper", task="work")])
        elif number == 2:
            assert ready.wait(5)
            target = outputs(body)[-1]
            value = tool_response(
                [
                    call(
                        "note",
                        "message_agent",
                        agent_id=target["agent_id"],
                        run_id=target["run_id"],
                        message_id="m1",
                        text="additional constraint",
                    )
                ]
            )
        else:
            if phase == "pending":
                pause()
            release.set()
            value = tool_response([call("wait", "wait_agents", agent_ids=[], timeout_seconds=30)])
        return httpx.Response(200, json=value)

    with application(root, parent, child) as (app, store):
        app.send("start")


if __name__ == "__main__":
    run(Path(sys.argv[1]), sys.argv[2])
