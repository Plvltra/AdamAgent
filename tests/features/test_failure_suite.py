"""B35.2：整族隔离、环境重置及真实B31运行器接入。"""

import json

import httpx
import pytest

from scripts.failure_suite import load_provider
from scripts.failure_tasks import generate
from scripts.task_eval import run_suite
from tests.features.test_task_eval_runner import config
from tests.features.test_tool_loop import answer


def test_family_split_fresh_reset_and_b31_execution_preserve_provenance(tmp_path):
    dataset = tmp_path / "dataset"
    generate(dataset)
    derive = load_provider(dataset / "dataset.json", "derive")
    holdout = load_provider(dataset / "dataset.json", "holdout")
    assert len(derive.cases) == 4 and len(holdout.cases) == 2
    assert not {c.rsplit("_", 1)[0] for c in derive.cases} & {
        c.rsplit("_", 1)[0] for c in holdout.cases
    }
    first = holdout.prepare(tmp_path / "first", holdout.cases[0])
    source = next(first.workspace.iterdir())
    source.write_text("changed")
    second = holdout.prepare(tmp_path / "second", holdout.cases[0])
    assert source.read_text() != (second.workspace / source.name).read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        holdout.prepare(tmp_path / "first", holdout.cases[0])
    roots = []

    def transport(task, name, iteration):
        roots.append(task.workspace)
        return httpx.MockTransport(
            lambda request: httpx.Response(200, json=answer(json.dumps(task.expected)))
        )

    results = tmp_path / "results"
    report = run_suite(
        results,
        {"base": config(tmp_path)},
        repeats=2,
        task_provider=holdout,
        transport_factory=transport,
    )
    assert report["passed"] == report["planned"] == len(set(roots)) == 4
    manifest = json.loads((results / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["task_provenance"] == holdout.provenance
    assert all(
        set(p.name for p in root.iterdir()) <= {"release_1.txt", "release_2.txt"} for root in roots
    )
