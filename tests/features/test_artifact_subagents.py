"""T8.2 主子应用使用各自附件来源；普通文件工具不能读取任何一方的附件副本。"""

import json
from dataclasses import replace

import httpx

from adam.config.settings import ToolLimits
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import application, call, outputs
from tests.features.test_tool_artifacts import populate, read_call
from tests.features.test_tool_loop import answer, tool_response


def test_parent_and_child_use_own_artifacts_without_cross_session_access(tmp_path):
    populate(tmp_path)
    parent_requests, child_requests = [], []
    parent_reference, child_reference = {}, {}
    paths = {}

    def child(request):
        body = json.loads(request.content)
        child_requests.append(body)
        phase = len(child_requests)
        if phase == 1:
            batch = [
                call("source", "read_file", path="artifact-source.txt"),
                read_call(parent_reference),
            ]
        elif phase == 2:
            values = outputs(body)
            assert values[-1]["ok"] is False and "执行来源" in values[-1]["error"]
            child_reference.update(values[-2]["artifact"])
            paths["child"] = next(
                (tmp_path / "sessions" / "_artifacts" / "children").glob("*/*/*.json")
            )
            batch = [
                read_call(child_reference, offset=1, count=90),
                call("ordinary-parent", "read_file", path=str(paths["parent"])),
            ]
        else:
            values = outputs(body)
            assert values[-2]["ok"] and values[-2]["text"]
            assert values[-1]["ok"] is False and "保留目录" in values[-1]["error"]
            return httpx.Response(200, json=answer(json.dumps(child_reference)))
        return httpx.Response(200, json=tool_response(batch))

    def parent(request):
        body = json.loads(request.content)
        parent_requests.append(body)
        phase = len(parent_requests)
        if phase == 1:
            batch = [call("source", "read_file", path="artifact-source.txt")]
        elif phase == 2:
            parent_reference.update(outputs(body)[-1]["artifact"])
            paths["parent"] = next((tmp_path / "sessions" / "_artifacts").glob("*/*.json"))
            batch = [
                call(
                    "spawn",
                    "spawn_agent",
                    name="reader",
                    task="Read text and own attachment",
                )
            ]
        elif phase == 3:
            batch = [call("wait", "wait_agents", agent_ids=[])]
        elif phase == 4:
            result = outputs(body)[-1]["agents"][0]
            assert result["status"] == "completed"
            assert json.loads(result["reply"]) == child_reference
            batch = [
                read_call(child_reference),
                call("ordinary-child", "read_file", path=str(paths["child"])),
            ]
        else:
            values = outputs(body)
            assert values[-2]["ok"] is False and "执行来源" in values[-2]["error"]
            assert values[-1]["ok"] is False and "保留目录" in values[-1]["error"]
            return httpx.Response(200, json=answer("isolated"))
        return httpx.Response(200, json=tool_response(batch))

    config = replace(config_at(tmp_path), tool_limits=ToolLimits(max_result_chars=1024))
    with application(tmp_path, parent, child, config=config) as (app, store):
        assert app.send("delegate a read") == "isolated"
        reference = store.load_artifact(app.session.session_id, parent_reference["artifact_id"])
        assert reference.source.session_id == app.session.session_id
        assert store.load_artifact(app.session.session_id, child_reference["artifact_id"]) is None
        assert paths["child"].parents[1].name == app.session.session_id
    assert len(parent_requests) == 5 and len(child_requests) == 3
