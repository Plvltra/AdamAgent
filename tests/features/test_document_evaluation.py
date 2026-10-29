"""固定评估的口径与独立 CLI；不请求聊天模型，也不自动下载 embedding。"""

import json
from pathlib import Path

import pytest

from adam.document_search import main
from adam.documents.evaluation import evaluate
from adam.documents.models import DocumentError


def test_evaluation_cli_writes_traceable_keyword_report(tmp_path):
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    output = tmp_path / "report.json"
    assert (
        main(
            [
                "--workspace",
                str(fixtures),
                "--root",
                "documents",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--output",
                str(output),
                "evaluate",
                str(fixtures / "document_questions.json"),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text(encoding="utf-8"))["keyword"]
    assert report["hits"] == 12
    assert report["answerable"] == 14
    assert report["no_answer_empty"] == report["no_answer"] == 2
    assert report["hit_at_k"] == 12 / 14
    assert len(report["rows"]) == 16
    assert not (tmp_path / "cache" / "models").exists()


@pytest.mark.parametrize(
    "cases",
    [
        [],
        [{"id": "missing"}],
        [{"id": "x", "kind": "answer", "query": "q", "path": None, "evidence": None}],
    ],
)
def test_invalid_evaluation_never_runs_queries(cases):
    def unexpected(*args):
        pytest.fail("invalid cases must not run retrieval")

    with pytest.raises(DocumentError):
        evaluate(unexpected, cases)
