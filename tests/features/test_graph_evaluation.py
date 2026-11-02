"""固定图对照的真实文件流程；仅模拟本地模型 HTTP。"""

import json
from pathlib import Path

import httpx
import pytest

from scripts import graph_evaluation as evaluation
from tests.features.test_tool_loop import answer


def test_fixed_comparison_preserves_failure_and_current_sources(tmp_path, monkeypatch):
    requests = []
    original = httpx.Client

    def handle(request):
        assert request.url.host == "127.0.0.1" and request.url.port == 8087
        body = json.loads(request.content)
        requests.append(body)
        assert not body.get("tools")
        if len(requests) == 3:
            # 模拟模型响应失败，固定题继续执行但不得重发这次请求。
            return httpx.Response(503, text="injected model failure")
        return httpx.Response(200, json=answer('{"answer": null, "evidence": []}'))

    monkeypatch.setattr(
        evaluation.httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"scope":"test only"}', encoding="utf-8")
    root = tmp_path / "batch"
    evaluation.run(root, manifest)
    rows = json.loads((root / "results.json").read_text(encoding="utf-8"))
    assert len(rows) == len(requests) == 10
    assert [(r["id"], r["arm"]) for r in rows] == [
        (c["id"], a) for c in evaluation.CASES for a in ("text", "graph")
    ]
    assert not rows[2]["request_success"]
    assert all(r["request_success"] for i, r in enumerate(rows) if i != 2)
    assert all(r["cost"] is None and r["currency"] is None for r in rows)
    assert rows[-1]["before_generation"] != rows[-1]["generation"]
    assert "team:platform" in json.dumps(rows[-1]["input"])
    assert "team:billing" not in json.dumps(rows[-1]["input"])
    assert len(rows[3]["input"]["sources"]) > len(rows[2]["input"]["sources"])
    saved = (root / "results.json").read_bytes()
    with pytest.raises(FileExistsError):
        evaluation.run(root, manifest)
    assert (root / "results.json").read_bytes() == saved and len(requests) == 10


def test_real_rows_reproduce_saved_scores():
    data = json.loads(
        (
            Path(__file__).resolve().parents[2] / "docs/evaluations/c08-graph-comparison.json"
        ).read_text(encoding="utf-8")
    )
    for row in data["results"]:
        result = evaluation.review_result(row)
        assert all(value == row["review"][key] for key, value in result.items())


def test_correct_answer_missing_one_edge_cannot_pass():
    row = {
        "request_success": True,
        "expected": "team:a",
        "required_sources": ["calls.md", "owners.md"],
        "input": {
            "sources": [
                {"chunk_id": "c", "path": "calls.md"},
                {"chunk_id": "o", "path": "owners.md"},
            ]
        },
        "message": {"content": '{"answer":"team:a","evidence":["o"]}'},
    }
    result = evaluation.review_result(row)
    assert result["answer_exact"] and result["citation_ids_valid"] and not result["complete_pass"]
    row["message"]["content"] = '{"answer":"team:a","evidence":["c","o"]}'
    assert evaluation.review_result(row)["complete_pass"]
    row["message"]["content"] = '{"answer":"team:a","evidence":["invented"]}'
    assert not evaluation.review_result(row)["citation_ids_valid"]
