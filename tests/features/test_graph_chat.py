"""C08.3：模拟远端模型，实际经过主子应用、文档授权、图工具和 JSONL。"""

import json

import httpx
import pytest

from tests.features.test_document_tools import config_at
from tests.features.test_graph_evidence import corpus_at
from tests.features.test_subagents import application, call, outputs
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("target", ["team:billing", "team:missing"])
def test_main_graph_tool_loop_saves_complete_evidence_or_explicit_missing(tmp_path, target):
    corpus_at(tmp_path)
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert "search_graph_documents" in {t["function"]["name"] for t in body["tools"]}
            result = tool_response(
                [
                    call(
                        "graph",
                        "search_graph_documents",
                        query="Checkout",
                        source_id="module:checkout",
                        target_id=target,
                    )
                ]
            )
        else:
            found = outputs(body)[-1]
            assert found["ok"]
            if target == "team:billing":
                assert len(found["paths"]) == 1 and len(found["paths"][0]) == 2
                assert {e["evidence"]["path"].rsplit("\\", 1)[-1] for e in found["paths"][0]} == {
                    "calls.md",
                    "owners.md",
                }
            else:
                assert found["status"] == "no_path" and found["paths"] == []
            result = answer("有来源" if found["paths"] else "没有关系依据")
        return httpx.Response(200, json=result)

    with application(tmp_path, handle, handle, config=config_at(tmp_path)) as (app, store):
        assert app.send("查明维护者") == ("有来源" if target == "team:billing" else "没有关系依据")
        history = store.load_messages(app.session.session_id)
        evidence = [json.loads(m.content) for m in history if m.role == "tool"][-1]
        assert evidence == outputs(requests[-1])[-1]
        assert len(requests) == 2
        assert not (config_at(tmp_path).sessions_dir / "_knowledge").exists()


def test_real_child_has_ordinary_graph_tool_and_parent_collects_original_sources(tmp_path):
    corpus_at(tmp_path)
    parents, children = [], []

    def parent(request):
        body = json.loads(request.content)
        parents.append(body)
        if len(parents) == 1:
            result = tool_response(
                [call("spawn", "spawn_agent", name="reader", task="查两跳维护关系")]
            )
        elif len(parents) == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            child = outputs(body)[-1]["agents"][0]
            assert child["status"] == "completed" and "team:billing" in child["reply"]
            result = answer(child["reply"])
        return httpx.Response(200, json=result)

    def child(request):
        body = json.loads(request.content)
        children.append(body)
        if len(children) == 1:
            result = tool_response(
                [
                    call(
                        "graph",
                        "search_graph_documents",
                        query="Checkout",
                        source_id="module:checkout",
                        target_id="team:billing",
                    )
                ]
            )
        else:
            found = outputs(body)[-1]
            assert found["ok"] and len(found["paths"][0]) == 2
            assert all(
                e["evidence"]["text"] and e["evidence"]["version"] for e in found["paths"][0]
            )
            result = answer("team:billing，依据 calls.md 与 owners.md 的原文关系。")
        return httpx.Response(200, json=result)

    with application(tmp_path, parent, child, config=config_at(tmp_path)) as (app, store):
        assert "team:billing" in app.send("委派只读关系查询")
        assert len(children) == 2 and len(parents) == 3
